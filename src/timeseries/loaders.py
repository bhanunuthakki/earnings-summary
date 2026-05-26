"""DB-aware loaders that materialize a Series for the primitives.

Two public functions, both best-effort (return [] when the DB is missing
or the query returns nothing — never raise):

  load_kpi_series(ticker, kpi_name, ...)        — kpi_facts JOIN kpi_definitions
  load_financial_series(ticker, line_item, ...) — financial_facts
  load_segment_series(ticker, segment, metric)  — segment_facts (bonus)

Dedup: financial_facts often has multiple rows per (ticker, line_item,
period_end, fiscal_period_type) — one per source_doc_id (FMP base statement,
FMP "as_reported", SEC XBRL, etc.). The loaders collapse those duplicates
by tier-aware preference: a deterministic SEC XBRL row always beats an
LLM-extracted row regardless of insertion order; within the same tier, the
highest id wins. See SOURCE_QUALITY_TIER_RANK below for the ordering.

Time-travel via `as_of_date`: when set, exclude any rows from documents
fetched after that date. Used by the audit-pass workflow to reproduce a
historical brief exactly. Falls back to "include all rows" when the
documents.fetched_at column is unreadable (older DBs).

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
from datetime import date, datetime
from pathlib import Path

from timeseries.primitives import Observation

log = logging.getLogger(__name__)

DEFAULT_PERIOD_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")

# Higher rank wins. Mirrors the documents.source_quality_tier enum
# in migration 0053. Unknown tiers fall back to 0 (lowest).
SOURCE_QUALITY_TIER_RANK: dict[str, int] = {
    "sec_official": 4,
    "fmp_normalized": 3,
    "llm_extracted": 2,
    "yfinance_fallback": 1,
}


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


def _normalize_as_of(as_of_date: date | datetime | str | None) -> str | None:
    """Coerce as_of_date to a sqlite-comparable 'YYYY-MM-DD 23:59:59' string,
    or None to disable the filter. We compare as strings because documents.
    fetched_at is stored as a sqlite TEXT/DATETIME — string compare works for
    the ISO formats used in this codebase."""
    if as_of_date is None:
        return None
    if isinstance(as_of_date, str):
        s = as_of_date.strip()
        # Accept either 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'; extend to
        # end-of-day so the filter is inclusive of fetches done that day.
        if len(s) == 10:
            return f"{s} 23:59:59"
        return s
    if isinstance(as_of_date, datetime):
        return as_of_date.strftime("%Y-%m-%d %H:%M:%S")
    # date (not datetime — class check order matters: datetime is a date)
    return f"{as_of_date.isoformat()} 23:59:59"


def _rows_to_series(rows: Iterable[sqlite3.Row]) -> list[Observation]:
    """Convert rows of (period_end, value) into an ascending Observation list,
    deduplicating by (period_end) — last value wins after the SQL has already
    chosen the canonical row via tier/id ordering."""
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


def _tier_rank_case_sql(column_alias: str) -> str:
    """Build a CASE expression mapping source_quality_tier text -> int rank.
    Used inline in SELECT for tier-aware ordering."""
    cases = " ".join(
        f"WHEN '{tier}' THEN {rank}"
        for tier, rank in SOURCE_QUALITY_TIER_RANK.items()
    )
    return f"CASE {column_alias} {cases} ELSE 0 END"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True iff PRAGMA reports `column` on `table`."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(str(r["name"]) == column for r in rows)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    """True iff `table` exists. Returns False on any sqlite error."""
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


# `latest_in_chain` lives in src/pipeline/restatement_detector.py — the
# loaders don't need a separate walker because the tier+id ordering in
# the SQL above already selects the most recent row per logical key.


def load_financial_series(
    ticker: str,
    line_item: str,
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    period_types: Iterable[str] = DEFAULT_PERIOD_TYPES,
    as_of_date: date | datetime | str | None = None,
) -> list[Observation]:
    """Load a chronological quarterly series for `ticker.line_item`.

    Per (period_end, fiscal_period_type) the winning row is the one with
    the highest source_quality_tier rank (sec_official > fmp_normalized >
    llm_extracted > yfinance_fallback); ties broken by max(id) so the most
    recently ingested fact wins.

    `as_of_date`: when set, exclude rows backed by documents fetched after
    that date — the loader produces the view that was knowable on that
    date. Pass a date, datetime, or 'YYYY-MM-DD' string.

    Returns [] when the DB is missing, the table doesn't exist, or no
    rows match.
    """
    resolved = _resolve_db_path(repo_root, db_path)
    if resolved is None:
        return []
    conn = _open(resolved)
    if conn is None:
        return []
    period_list = list(period_types) or list(DEFAULT_PERIOD_TYPES)
    placeholders = ",".join("?" * len(period_list))
    as_of_cutoff = _normalize_as_of(as_of_date)
    try:
        if not _has_table(conn, "financial_facts"):
            return []
        has_documents = _has_table(conn, "documents")
        # On legacy schemas (no documents table, or no tier column), fall
        # back to the pre-0053 max(id) dedup so callers operating against
        # bare fixture DBs keep working.
        has_tier = (
            has_documents
            and _has_column(conn, "documents", "source_quality_tier")
        )
        if not has_documents:
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

        rank_expr = (
            _tier_rank_case_sql("d.source_quality_tier") if has_tier else "0"
        )
        as_of_clause = (
            "AND d.fetched_at <= ? "
            if as_of_cutoff is not None
            else ""
        )
        as_of_params: tuple[object, ...] = (
            (as_of_cutoff,) if as_of_cutoff is not None else ()
        )
        # Pick the winning row per logical (period_end, fiscal_period_type)
        # tuple by ranking on (tier, id). Implemented via a correlated
        # subquery that emits the chosen id; the outer query then reads
        # value off that row.
        rows = conn.execute(
            f"""
            SELECT ff.period_end, ff.value
            FROM financial_facts ff
            JOIN documents d ON d.id = ff.source_doc_id
            WHERE ff.ticker = ?
              AND ff.line_item = ?
              AND ff.fiscal_period_type IN ({placeholders})
              {as_of_clause}
              AND ff.id = (
                SELECT ff2.id
                FROM financial_facts ff2
                JOIN documents d2 ON d2.id = ff2.source_doc_id
                WHERE ff2.ticker = ff.ticker
                  AND ff2.line_item = ff.line_item
                  AND ff2.period_end = ff.period_end
                  AND ff2.fiscal_period_type = ff.fiscal_period_type
                  {as_of_clause.replace("d.", "d2.")}
                ORDER BY {rank_expr.replace("d.", "d2.")} DESC, ff2.id DESC
                LIMIT 1
              )
            ORDER BY ff.period_end ASC
            """,
            (
                ticker.upper(),
                line_item,
                *period_list,
                *as_of_params,
                *as_of_params,
            ),
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
    as_of_date: date | datetime | str | None = None,
) -> list[Observation]:
    """Load a chronological quarterly series for `ticker.kpi_name`.

    JOIN kpi_facts.kpi_definition_id = kpi_definitions.id WHERE kd.name = ?
    so the caller specifies the human-readable KPI name (the same string
    used in holdings JSON tier_1_kpis). Tier-aware dedup + as_of_date as
    in `load_financial_series`.
    """
    resolved = _resolve_db_path(repo_root, db_path)
    if resolved is None:
        return []
    conn = _open(resolved)
    if conn is None:
        return []
    period_list = list(period_types) or list(DEFAULT_PERIOD_TYPES)
    placeholders = ",".join("?" * len(period_list))
    as_of_cutoff = _normalize_as_of(as_of_date)
    try:
        for required in ("kpi_facts", "kpi_definitions"):
            if not _has_table(conn, required):
                return []
        has_documents = _has_table(conn, "documents")
        if not has_documents:
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

        has_tier = _has_column(conn, "documents", "source_quality_tier")
        rank_expr = (
            _tier_rank_case_sql("d.source_quality_tier") if has_tier else "0"
        )
        as_of_clause = (
            "AND d.fetched_at <= ? "
            if as_of_cutoff is not None
            else ""
        )
        as_of_params: tuple[object, ...] = (
            (as_of_cutoff,) if as_of_cutoff is not None else ()
        )
        rows = conn.execute(
            f"""
            SELECT kf.period_end, kf.value
            FROM kpi_facts kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            JOIN documents d ON d.id = kf.source_doc_id
            WHERE kf.ticker = ?
              AND kd.name = ?
              AND kf.fiscal_period_type IN ({placeholders})
              {as_of_clause}
              AND kf.id = (
                SELECT kf2.id
                FROM kpi_facts kf2
                JOIN documents d2 ON d2.id = kf2.source_doc_id
                WHERE kf2.ticker = kf.ticker
                  AND kf2.kpi_definition_id = kf.kpi_definition_id
                  AND kf2.period_end = kf.period_end
                  AND kf2.fiscal_period_type = kf.fiscal_period_type
                  {as_of_clause.replace("d.", "d2.")}
                ORDER BY {rank_expr.replace("d.", "d2.")} DESC, kf2.id DESC
                LIMIT 1
              )
            ORDER BY kf.period_end ASC
            """,
            (
                ticker.upper(),
                kpi_name,
                *period_list,
                *as_of_params,
                *as_of_params,
            ),
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
    as_of_date: date | datetime | str | None = None,
) -> list[Observation]:
    """Load a segment-level series (e.g. Cloud revenue, Family-of-Apps OI).

    segment_facts is keyed on (ticker, period_end, segment_name, metric)
    so we filter on all three. Tier-aware dedup + as_of_date as in
    `load_financial_series`. segment_facts has no `supersedes_id` column
    (out of scope this PR) — the ranking is purely (tier, id).
    """
    resolved = _resolve_db_path(repo_root, db_path)
    if resolved is None:
        return []
    conn = _open(resolved)
    if conn is None:
        return []
    period_list = list(period_types) or list(DEFAULT_PERIOD_TYPES)
    placeholders = ",".join("?" * len(period_list))
    as_of_cutoff = _normalize_as_of(as_of_date)
    try:
        if not _has_table(conn, "segment_facts"):
            return []
        has_documents = _has_table(conn, "documents")
        if not has_documents:
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

        has_tier = _has_column(conn, "documents", "source_quality_tier")
        rank_expr = (
            _tier_rank_case_sql("d.source_quality_tier") if has_tier else "0"
        )
        as_of_clause = (
            "AND d.fetched_at <= ? "
            if as_of_cutoff is not None
            else ""
        )
        as_of_params: tuple[object, ...] = (
            (as_of_cutoff,) if as_of_cutoff is not None else ()
        )
        rows = conn.execute(
            f"""
            SELECT sf.period_end, sf.value
            FROM segment_facts sf
            JOIN documents d ON d.id = sf.source_doc_id
            WHERE sf.ticker = ?
              AND sf.segment_name = ?
              AND sf.metric = ?
              AND sf.fiscal_period_type IN ({placeholders})
              {as_of_clause}
              AND sf.id = (
                SELECT sf2.id
                FROM segment_facts sf2
                JOIN documents d2 ON d2.id = sf2.source_doc_id
                WHERE sf2.ticker = sf.ticker
                  AND sf2.segment_name = sf.segment_name
                  AND sf2.metric = sf.metric
                  AND sf2.period_end = sf.period_end
                  AND sf2.fiscal_period_type = sf.fiscal_period_type
                  {as_of_clause.replace("d.", "d2.")}
                ORDER BY {rank_expr.replace("d.", "d2.")} DESC, sf2.id DESC
                LIMIT 1
              )
            ORDER BY sf.period_end ASC
            """,
            (
                ticker.upper(),
                segment_name,
                metric,
                *period_list,
                *as_of_params,
                *as_of_params,
            ),
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
