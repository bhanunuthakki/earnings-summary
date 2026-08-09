"""DB-aware loaders that materialize a Series for the primitives.

Public functions, all best-effort (return [] when the DB is missing
or the query returns nothing — never raise):

  load_kpi_series(ticker, kpi_name, ...)             — kpi_facts JOIN kpi_definitions
  load_financial_series(ticker, line_item, ...)      — financial_facts
  load_financial_series_with_provenance(...)         — same pick + per-row provenance (P5.1)
  load_kpi_series_with_provenance(...)               — kpi sibling of the above (P5.1)
  load_segment_junction_series(ticker, dims, metric) — segment_periods + segment_dimensions (cross-tab)
  load_segment_series(ticker, segment, metric)       — thin shim that maps the legacy
                                                       segment_facts metric vocabulary to the
                                                       junction and delegates to
                                                       load_segment_junction_series

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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from provenance.financial_fact_resolution import canonical_fact_relation
from provenance.overrides import (
    FINANCIAL_FACT,
    KPI,
    OverrideAction,
    chip_override_map,
    date_override_map,
    override_provenance,
    qualify_note,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from timeseries.primitives import Observation

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourcedObservation:
    """One observation plus the provenance of its winning fact row.

    ``provenance`` carries the same key set as
    :func:`load_financial_cell_provenance`'s per-cell payload (source,
    fact_id, source_doc_id, fetched_at, source_url, doc_type,
    accession_number, filing_date, locator) so chip builders consume one
    shape regardless of which loader produced the cell.
    """

    period_end: datetime
    value: float
    unit: str | None
    provenance: dict[str, object]


DEFAULT_PERIOD_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")

# Higher rank wins. Mirrors the documents.source_quality_tier enum
# in migration 0053. Unknown tiers fall back to 0 (lowest).
#
# `s1_provisional` is audited data lifted from an S-1 before the issuer files
# any 10-Q/10-K; it sits at rank 0 so a real FMP/SEC row always supersedes it
# for the same logical key. (Rank 0 also happens to be the unknown-tier
# fallback, but it's listed explicitly so the intent survives a refactor.)
SOURCE_QUALITY_TIER_RANK: dict[str, int] = {
    "sec_official": 4,
    "fmp_normalized": 3,
    "llm_extracted": 2,
    "yfinance_fallback": 1,
    "s1_provisional": 0,
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
        return connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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


def _overlay_scalar_series(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fact_kind: str,
    fact_key: str,
    period_types: Iterable[str],
    series: list[Observation],
) -> list[Observation]:
    """Apply active company-doc overrides to a resolved (period_end, value) series.

    A ``replace`` substitutes the value with the company-published figure; a ``drop``
    omits the period. A no-op when there are no overrides (the common case) so the
    read path is unaffected. This is how a company-doc figure wins over FMP at the
    canonical series read paths, durably (survives an FMP re-fetch). See
    ``directives/provenance_override_2026_06.md``.
    """
    date_map = date_override_map(
        conn, ticker=ticker, fact_kind=fact_kind, fact_key=fact_key, period_types=period_types
    )
    if not date_map:
        return series
    out: list[Observation] = []
    for obs in series:
        ov = date_map.get(str(obs.period_end)[:10])
        if ov is None:
            out.append(obs)
        elif ov.action != OverrideAction.DROP.value:
            value = float(ov.value) if ov.value is not None else obs.value
            out.append(Observation(period_end=obs.period_end, value=value))
    return out


def _overlay_sourced_series(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fact_kind: str,
    fact_key: str,
    period_types: Iterable[str],
    series: list[SourcedObservation],
) -> list[SourcedObservation]:
    """The provenance sibling of :func:`_overlay_scalar_series`.

    A ``replace`` substitutes the value (and unit) with the company-published
    figure AND swaps the cell provenance to the company document
    (``override_provenance``), so the source chip describes the WINNING row, not
    the FMP row whose value was superseded — the value/chip divergence these
    ``_with_provenance`` loaders exist to prevent. A ``drop`` omits the period;
    ``qualify`` is excluded by ``date_override_map`` (annotation only, no value
    change). A no-op when there are no overrides (the common case).
    """
    date_map = date_override_map(
        conn, ticker=ticker, fact_kind=fact_kind, fact_key=fact_key, period_types=period_types
    )
    if not date_map:
        return series
    out: list[SourcedObservation] = []
    for obs in series:
        ov = date_map.get(str(obs.period_end)[:10])
        if ov is None:
            out.append(obs)
            continue
        if ov.action == OverrideAction.DROP.value:
            continue
        prov = dict(obs.provenance)
        prov.update(override_provenance(ov))
        out.append(
            SourcedObservation(
                period_end=obs.period_end,
                value=float(ov.value) if ov.value is not None else obs.value,
                unit=ov.unit if ov.unit is not None else obs.unit,
                provenance=prov,
            )
        )
    return out


def tier_rank_case_sql(column_alias: str) -> str:
    """Build a CASE expression mapping source_quality_tier text -> int rank.

    Public so the reader families that can't route through the Series loaders
    (cockpit_fundamentals, report §3 financials, fmp_derived_kpis, ask.grounding)
    can replicate the loaders' EXACT ``(tier_rank, id)`` winner ordering in their
    own SQL rather than diverging into a source-agnostic ``source_doc_id DESC``
    pick. ``column_alias`` is the SQL reference to ``documents.source_quality_tier``
    in the caller's query (e.g. ``"d.source_quality_tier"``). Unknown/NULL tiers
    fall to rank 0, matching :data:`SOURCE_QUALITY_TIER_RANK`'s fallback."""
    cases = " ".join(
        f"WHEN '{tier}' THEN {rank}" for tier, rank in SOURCE_QUALITY_TIER_RANK.items()
    )
    return f"CASE {column_alias} {cases} ELSE 0 END"


# Back-compat alias for the pre-public name (internal callers below).
_tier_rank_case_sql = tier_rank_case_sql


def _probe_has_table(conn: sqlite3.Connection, table: str) -> bool:
    """row_factory-independent table probe for the reader helpers.

    The Series loaders' ``_has_table``/``_has_column`` read ``r["name"]``, which
    needs ``conn.row_factory = sqlite3.Row``. The reader families call the
    ``reader_tier_*`` helpers with whatever connection they hold — research
    cockpit / the refresh script open a bare (tuple-row) connection — so these
    probes use positional access and work regardless of row_factory."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _probe_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """row_factory-independent column probe (see :func:`_probe_has_table`).
    PRAGMA table_info returns (cid, name, type, ...); name is index 1."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(r[1] == column for r in rows)


def reader_tier_rank_sql(
    conn: sqlite3.Connection,
    *,
    tier_column: str = "d.source_quality_tier",
    documents_table: str = "documents",
) -> str:
    """Tier-rank ORDER-BY fragment for a reader query, guarded for legacy DBs.

    Returns :func:`tier_rank_case_sql` when ``documents.source_quality_tier``
    exists, else the constant ``"NULL"`` so the caller's ``ORDER BY <this> DESC,
    <table>.id DESC`` collapses to the pre-0053 ``id``-only pick on fixture DBs
    that predate the tier column (or lack the ``documents`` table entirely). The
    same degrade the Series loaders apply via their ``has_tier`` branch — factored
    out so the four reader families that replicate the ordering in raw SQL
    (cockpit_fundamentals, report §3 financials, fmp_derived_kpis, ask.grounding)
    stay column-safe without duplicating the PRAGMA probe.

    The degraded value is ``"NULL"``, NOT ``"0"``: a bare integer in an
    ``ORDER BY`` is a POSITIONAL COLUMN REFERENCE in SQLite (``ORDER BY 0`` →
    "term out of range"), whereas ``NULL`` is a constant that sorts all rows
    equal so the ``id`` tiebreak takes over cleanly."""
    if _probe_has_table(conn, documents_table) and _probe_has_column(
        conn, documents_table, "source_quality_tier"
    ):
        return tier_rank_case_sql(tier_column)
    return "NULL"


def reader_tier_join_sql(
    conn: sqlite3.Connection,
    *,
    fact_alias: str = "ff",
    doc_alias: str = "d",
    documents_table: str = "documents",
) -> str:
    """The ``LEFT JOIN documents`` clause a tier-aware reader query needs, or
    ``""`` when the ``documents`` table is absent.

    Paired with :func:`reader_tier_rank_sql`: when documents is missing the rank
    fragment is the constant ``"NULL"`` AND this join is empty, so the query
    never references the ``d`` alias it can't resolve — it degrades cleanly to
    the id-only pick over ``financial_facts`` alone (the pre-0053 behavior). When
    documents exists but the tier column doesn't, the join is still emitted
    (harmless) and the rank fragment is ``"NULL"``."""
    if _probe_has_table(conn, documents_table):
        return f"LEFT JOIN {documents_table} {doc_alias} ON {doc_alias}.id = {fact_alias}.source_doc_id"
    return ""


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
        fact_relation = canonical_fact_relation(conn, "financial_facts").sql
        has_documents = _has_table(conn, "documents")
        # On legacy schemas (no documents table, or no tier column), fall
        # back to the pre-0053 max(id) dedup so callers operating against
        # bare fixture DBs keep working.
        has_tier = has_documents and _has_column(conn, "documents", "source_quality_tier")
        if not has_documents:
            rows = conn.execute(
                f"""
                SELECT ff.period_end, ff.value
                FROM {fact_relation} ff
                WHERE ff.ticker = ?
                  AND ff.line_item = ?
                  AND ff.fiscal_period_type IN ({placeholders})
                  AND ff.id = (
                    SELECT MAX(ff2.id) FROM {fact_relation} ff2
                    WHERE ff2.ticker = ff.ticker
                      AND ff2.line_item = ff.line_item
                      AND ff2.period_end = ff.period_end
                      AND ff2.fiscal_period_type = ff.fiscal_period_type
                  )
                ORDER BY ff.period_end ASC
                """,
                (ticker.upper(), line_item, *period_list),
            ).fetchall()
            return _overlay_scalar_series(
                conn,
                ticker=ticker,
                fact_kind=FINANCIAL_FACT,
                fact_key=line_item,
                period_types=period_list,
                series=_rows_to_series(rows),
            )

        rank_expr = _tier_rank_case_sql("d.source_quality_tier") if has_tier else "0"
        as_of_clause = "AND d.fetched_at <= ? " if as_of_cutoff is not None else ""
        as_of_params: tuple[object, ...] = (as_of_cutoff,) if as_of_cutoff is not None else ()
        # Pick the winning row per logical (period_end, fiscal_period_type)
        # tuple by ranking on (tier, id). Implemented via a correlated
        # subquery that emits the chosen id; the outer query then reads
        # value off that row.
        rows = conn.execute(
            f"""
            SELECT ff.period_end, ff.value
            FROM {fact_relation} ff
            JOIN documents d ON d.id = ff.source_doc_id
            WHERE ff.ticker = ?
              AND ff.line_item = ?
              AND ff.fiscal_period_type IN ({placeholders})
              {as_of_clause}
              AND ff.id = (
                SELECT ff2.id
                FROM {fact_relation} ff2
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
        return _overlay_scalar_series(
            conn,
            ticker=ticker,
            fact_kind=FINANCIAL_FACT,
            fact_key=line_item,
            period_types=period_list,
            series=_rows_to_series(rows),
        )
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


def load_financial_fact_provenance(
    ticker: str,
    line_item: str,
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    period_types: Iterable[str] = DEFAULT_PERIOD_TYPES,
    as_of_date: date | datetime | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, object] | None:
    """Return tier-aware provenance for the latest-period row of `ticker.line_item`.

    Mirrors `load_financial_series`'s row-picking — same tier+id ordering
    (sec_official > fmp_normalized > llm_extracted > yfinance_fallback;
    ties broken by max(id)) — but instead of materializing a Series of
    values returns the chosen row's provenance metadata for the LATEST
    period_end. Shape:

        {"source": "sec_official",
         "fact_id": 12345,
         "source_doc_id": 67,
         "fetched_at": "2026-05-26 14:00:00",
         "period_end": "2025-03-31",
         "fiscal_period_type": "Q1",
         "value": 1234567.0}

    Returns None when the DB is missing, the financial_facts table
    doesn't exist, or no rows match. On legacy schemas (no documents
    table or no source_quality_tier column), `source` falls back to
    documents.source_type when available or "unknown" otherwise — the
    chosen row id is still meaningful via the max(id) fallback.

    Intended for the brief_provenance_log per-metric audit trail; not on
    the hot path of `detect_trend` / `yoy_acceleration` etc.
    """
    db_conn = conn
    if db_conn is None:
        resolved = _resolve_db_path(repo_root, db_path)
        if resolved is None:
            return None
        db_conn = _open(resolved)
        if db_conn is None:
            return None
    period_list = list(period_types) or list(DEFAULT_PERIOD_TYPES)
    placeholders = ",".join("?" * len(period_list))
    as_of_cutoff = _normalize_as_of(as_of_date)
    try:
        if not _has_table(db_conn, "financial_facts"):
            return None
        has_documents = _has_table(db_conn, "documents")
        if not has_documents:
            row = db_conn.execute(
                f"""
                SELECT ff.id AS fact_id,
                       ff.source_doc_id,
                       ff.period_end,
                       ff.fiscal_period_type,
                       ff.value
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
                ORDER BY ff.period_end DESC
                LIMIT 1
                """,
                (ticker.upper(), line_item, *period_list),
            ).fetchone()
            if row is None:
                return None
            return {
                "source": "unknown",
                "fact_id": int(row["fact_id"]),
                "source_doc_id": int(row["source_doc_id"]),
                "fetched_at": None,
                "period_end": str(row["period_end"])[:10],
                "fiscal_period_type": str(row["fiscal_period_type"]),
                "value": float(row["value"]),
            }

        has_tier = _has_column(db_conn, "documents", "source_quality_tier")
        rank_expr = _tier_rank_case_sql("d.source_quality_tier") if has_tier else "0"
        as_of_clause = "AND d.fetched_at <= ? " if as_of_cutoff is not None else ""
        as_of_params: tuple[object, ...] = (as_of_cutoff,) if as_of_cutoff is not None else ()
        tier_select = (
            "d.source_quality_tier AS source"
            if has_tier
            else "COALESCE(d.source_type, 'unknown') AS source"
        )
        row = db_conn.execute(
            f"""
            SELECT ff.id AS fact_id,
                   ff.source_doc_id,
                   ff.period_end,
                   ff.fiscal_period_type,
                   ff.value,
                   d.fetched_at,
                   {tier_select}
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
            ORDER BY ff.period_end DESC
            LIMIT 1
            """,
            (
                ticker.upper(),
                line_item,
                *period_list,
                *as_of_params,
                *as_of_params,
            ),
        ).fetchone()
        if row is None:
            return None
        fetched_at_raw = row["fetched_at"]
        return {
            "source": str(row["source"]) if row["source"] is not None else "unknown",
            "fact_id": int(row["fact_id"]),
            "source_doc_id": int(row["source_doc_id"]),
            "fetched_at": str(fetched_at_raw) if fetched_at_raw is not None else None,
            "period_end": str(row["period_end"])[:10],
            "fiscal_period_type": str(row["fiscal_period_type"]),
            "value": float(row["value"]),
        }
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "timeseries_load_financial_provenance_failed",
                "ticker": ticker,
                "line_item": line_item,
                "error": str(exc),
            }
        )
        return None
    finally:
        if conn is None:
            db_conn.close()


def load_financial_cell_provenance(
    ticker: str,
    line_items: Iterable[str],
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    period_types: Iterable[str] = DEFAULT_PERIOD_TYPES,
) -> dict[str, dict[str, dict[str, object]]]:
    """Per-cell provenance for the report source chips (P3.3).

    Returns ``{line_item: {"YYYY-MM-DD": prov}}`` where prov carries the
    winning row's source identity::

        {"source": "<source_quality_tier or source_type or 'unknown'>",
         "fetched_at": "...", "source_url": ..., "doc_type": ...,
         "accession_number": ..., "filing_date": ..., "locator": ...,
         "fact_id": 123, "source_doc_id": 45}

    Row pick deliberately matches the ``metrics`` VIEW the Financials
    section renders from — highest ``source_doc_id`` per logical
    (line_item, period_end) tuple, ties broken by max(id) — NOT the
    tier-aware pick the Series loaders use, so the chip always describes
    the number actually displayed. (The view and the tier pick agree in
    the overwhelmingly common single-source case; where they diverge the
    displayed number wins.)

    Best-effort like every loader: ``{}`` when the DB or tables are
    missing; columns absent on legacy schemas (locator/accession: 0075,
    tier: 0054) come back as None.
    """
    resolved = _resolve_db_path(repo_root, db_path)
    if resolved is None:
        return {}
    conn = _open(resolved)
    if conn is None:
        return {}
    items = [li for li in line_items if li]
    if not items:
        conn.close()
        return {}
    period_list = list(period_types) or list(DEFAULT_PERIOD_TYPES)
    p_marks = ",".join("?" * len(period_list))
    li_marks = ",".join("?" * len(items))
    try:
        if not _has_table(conn, "financial_facts") or not _has_table(conn, "documents"):
            return {}
        tier_select = (
            "d.source_quality_tier AS source"
            if _has_column(conn, "documents", "source_quality_tier")
            else "COALESCE(d.source_type, 'unknown') AS source"
        )
        locator_select = (
            "ff.locator" if _has_column(conn, "financial_facts", "locator") else "NULL AS locator"
        )
        accession_select = (
            "d.accession_number, d.filing_date"
            if _has_column(conn, "documents", "accession_number")
            else "NULL AS accession_number, NULL AS filing_date"
        )
        confidence_select = (
            "ff.confidence"
            if _has_column(conn, "financial_facts", "confidence")
            else "NULL AS confidence"
        )
        extracted_by_select = (
            "ff.extracted_by"
            if _has_column(conn, "financial_facts", "extracted_by")
            else "NULL AS extracted_by"
        )
        rows = conn.execute(
            f"""
            SELECT ff.id AS fact_id,
                   ff.source_doc_id,
                   ff.line_item,
                   ff.period_end,
                   {locator_select},
                   {confidence_select},
                   {extracted_by_select},
                   d.fetched_at,
                   d.source_url,
                   d.doc_type,
                   {accession_select},
                   {tier_select}
            FROM financial_facts ff
            JOIN documents d ON d.id = ff.source_doc_id
            WHERE ff.ticker = ?
              AND ff.line_item IN ({li_marks})
              AND ff.fiscal_period_type IN ({p_marks})
              AND ff.id = (
                SELECT ff2.id
                FROM financial_facts ff2
                WHERE ff2.ticker = ff.ticker
                  AND ff2.line_item = ff.line_item
                  AND ff2.period_end = ff.period_end
                  AND ff2.fiscal_period_type = ff.fiscal_period_type
                ORDER BY ff2.source_doc_id DESC, ff2.id DESC
                LIMIT 1
              )
            """,
            (ticker.upper(), *items, *period_list),
        ).fetchall()
        out: dict[str, dict[str, dict[str, object]]] = {}
        for r in rows:
            pe = _parse_period_end(r["period_end"])
            if pe is None:
                continue
            prov: dict[str, object] = {
                "source": str(r["source"]) if r["source"] is not None else "unknown",
                "fact_id": int(r["fact_id"]),
                "source_doc_id": int(r["source_doc_id"]),
                "fetched_at": str(r["fetched_at"]) if r["fetched_at"] is not None else None,
                "source_url": str(r["source_url"]) if r["source_url"] is not None else None,
                "doc_type": str(r["doc_type"]) if r["doc_type"] is not None else None,
                "accession_number": (
                    str(r["accession_number"]) if r["accession_number"] is not None else None
                ),
                "filing_date": str(r["filing_date"]) if r["filing_date"] is not None else None,
                "locator": str(r["locator"]) if r["locator"] is not None else None,
                "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                "extracted_by": str(r["extracted_by"]) if r["extracted_by"] is not None else None,
            }
            out.setdefault(str(r["line_item"]), {})[pe.date().isoformat()] = prov
        # Provenance-override surfacing (P6): a company-doc `replace` swaps each
        # overridden cell's source to the filing it cites; a `qualify` adds a ⚠ note
        # (merged into issues by _to_cell_source). Keeps the chip honest.
        for line_item, by_period in out.items():
            ov_map = chip_override_map(
                conn,
                ticker=ticker,
                fact_kind=FINANCIAL_FACT,
                fact_key=line_item,
                period_types=period_list,
            )
            for period_iso, ov in ov_map.items():
                cell = by_period.get(period_iso)
                if cell is None:
                    continue
                if ov.action == OverrideAction.REPLACE.value:
                    cell.update(override_provenance(ov))
                elif ov.action == OverrideAction.QUALIFY.value:
                    cell["issues"] = [qualify_note(ov)]
        return out
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "timeseries_load_cell_provenance_failed",
                "ticker": ticker,
                "error": str(exc),
            }
        )
        return {}
    finally:
        conn.close()


def _sourced_rows(
    rows: Iterable[sqlite3.Row], *, fact_table: str = "financial_facts"
) -> list[SourcedObservation]:
    """Rows carrying (period_end, value, unit, + provenance columns) → an
    ascending SourcedObservation list, deduplicating by period_end with the
    last row winning (the SQL has already chosen the canonical row per
    logical key via tier/id ordering).

    ``fact_table`` names the table ``fact_id`` indexes (financial_facts vs
    kpi_facts) so the chip's provenance peek can build the right
    ``<table>:<id>`` fact_ref (provenance click-through Phase B)."""
    by_period: dict[datetime, SourcedObservation] = {}
    for r in rows:
        pe = _parse_period_end(r["period_end"])
        if pe is None:
            continue
        try:
            v = float(r["value"])
        except (TypeError, ValueError):
            continue
        prov: dict[str, object] = {
            "source": str(r["source"]) if r["source"] is not None else "unknown",
            "fact_id": int(r["fact_id"]),
            "fact_table": fact_table,
            "source_doc_id": int(r["source_doc_id"]),
            "fetched_at": str(r["fetched_at"]) if r["fetched_at"] is not None else None,
            "source_url": str(r["source_url"]) if r["source_url"] is not None else None,
            "doc_type": str(r["doc_type"]) if r["doc_type"] is not None else None,
            "accession_number": (
                str(r["accession_number"]) if r["accession_number"] is not None else None
            ),
            "filing_date": str(r["filing_date"]) if r["filing_date"] is not None else None,
            "locator": str(r["locator"]) if r["locator"] is not None else None,
            "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
            "extracted_by": str(r["extracted_by"]) if r["extracted_by"] is not None else None,
            "computed_from": str(r["computed_from"]) if r["computed_from"] is not None else None,
        }
        unit_raw = r["unit"]
        by_period[pe] = SourcedObservation(
            period_end=pe,
            value=v,
            unit=str(unit_raw) if unit_raw is not None else None,
            provenance=prov,
        )
    return [by_period[pe] for pe in sorted(by_period)]


def load_financial_series_with_provenance(
    ticker: str,
    line_item: str,
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    period_types: Iterable[str] = DEFAULT_PERIOD_TYPES,
) -> list[SourcedObservation]:
    """`load_financial_series` + per-observation provenance in ONE query.

    The ViewSpec engine (master build P5.1) renders every cell with a
    source chip; loading values and provenance separately would risk the
    chip describing a different row than the displayed number when the
    tier-aware pick and the cell-provenance pick diverge (see
    `load_financial_cell_provenance`'s docstring). This loader uses the
    SAME tier+id ordering as `load_financial_series`, so the value and its
    chip always come from the same winning fact row.

    Requires the documents table (provenance is the point): returns []
    when it — or financial_facts — is missing. Columns absent on legacy
    schemas (locator/accession: 0075, tier: 0054) come back as None.
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
        if not _has_table(conn, "financial_facts") or not _has_table(conn, "documents"):
            return []
        has_tier = _has_column(conn, "documents", "source_quality_tier")
        rank_expr = _tier_rank_case_sql("d.source_quality_tier") if has_tier else "0"
        tier_select = (
            "d.source_quality_tier AS source"
            if has_tier
            else "COALESCE(d.source_type, 'unknown') AS source"
        )
        locator_select = (
            "ff.locator" if _has_column(conn, "financial_facts", "locator") else "NULL AS locator"
        )
        accession_select = (
            "d.accession_number, d.filing_date"
            if _has_column(conn, "documents", "accession_number")
            else "NULL AS accession_number, NULL AS filing_date"
        )
        unit_select = "ff.unit" if _has_column(conn, "financial_facts", "unit") else "NULL AS unit"
        confidence_select = (
            "ff.confidence"
            if _has_column(conn, "financial_facts", "confidence")
            else "NULL AS confidence"
        )
        extracted_by_select = (
            "ff.extracted_by"
            if _has_column(conn, "financial_facts", "extracted_by")
            else "NULL AS extracted_by"
        )
        rows = conn.execute(
            f"""
            SELECT ff.period_end,
                   ff.value,
                   {unit_select},
                   ff.id AS fact_id,
                   ff.source_doc_id,
                   {locator_select},
                   {confidence_select},
                   {extracted_by_select},
                   NULL AS computed_from,
                   d.fetched_at,
                   d.source_url,
                   d.doc_type,
                   {accession_select},
                   {tier_select}
            FROM financial_facts ff
            JOIN documents d ON d.id = ff.source_doc_id
            WHERE ff.ticker = ?
              AND ff.line_item = ?
              AND ff.fiscal_period_type IN ({placeholders})
              AND ff.id = (
                SELECT ff2.id
                FROM financial_facts ff2
                JOIN documents d2 ON d2.id = ff2.source_doc_id
                WHERE ff2.ticker = ff.ticker
                  AND ff2.line_item = ff.line_item
                  AND ff2.period_end = ff.period_end
                  AND ff2.fiscal_period_type = ff.fiscal_period_type
                ORDER BY {rank_expr.replace("d.", "d2.")} DESC, ff2.id DESC
                LIMIT 1
              )
            ORDER BY ff.period_end ASC
            """,
            (ticker.upper(), line_item, *period_list),
        ).fetchall()
        return _overlay_sourced_series(
            conn,
            ticker=ticker,
            fact_kind=FINANCIAL_FACT,
            fact_key=line_item,
            period_types=period_list,
            series=_sourced_rows(rows),
        )
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "timeseries_load_financial_sourced_failed",
                "ticker": ticker,
                "line_item": line_item,
                "error": str(exc),
            }
        )
        return []
    finally:
        conn.close()


def load_kpi_series_with_provenance(
    ticker: str,
    kpi_name: str,
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    period_types: Iterable[str] = DEFAULT_PERIOD_TYPES,
) -> list[SourcedObservation]:
    """`load_kpi_series` + per-observation provenance in ONE query.

    The kpi_facts sibling of `load_financial_series_with_provenance`,
    closing the seam P3.3 left (kpi cells rode CellSource but their loader
    path never emitted document identity). Same tier+id row pick as
    `load_kpi_series`; same provenance payload shape as the financial
    loaders. Requires kpi_facts + kpi_definitions + documents; returns []
    when any is missing.
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
        for required in ("kpi_facts", "kpi_definitions", "documents"):
            if not _has_table(conn, required):
                return []
        has_tier = _has_column(conn, "documents", "source_quality_tier")
        rank_expr = _tier_rank_case_sql("d.source_quality_tier") if has_tier else "0"
        tier_select = (
            "d.source_quality_tier AS source"
            if has_tier
            else "COALESCE(d.source_type, 'unknown') AS source"
        )
        locator_select = (
            "kf.locator" if _has_column(conn, "kpi_facts", "locator") else "NULL AS locator"
        )
        accession_select = (
            "d.accession_number, d.filing_date"
            if _has_column(conn, "documents", "accession_number")
            else "NULL AS accession_number, NULL AS filing_date"
        )
        unit_select = "kf.unit" if _has_column(conn, "kpi_facts", "unit") else "NULL AS unit"
        confidence_select = (
            "kf.confidence"
            if _has_column(conn, "kpi_facts", "confidence")
            else "NULL AS confidence"
        )
        extracted_by_select = (
            "kf.extracted_by"
            if _has_column(conn, "kpi_facts", "extracted_by")
            else "NULL AS extracted_by"
        )
        computed_from_select = (
            "kf.computed_from"
            if _has_column(conn, "kpi_facts", "computed_from")
            else "NULL AS computed_from"
        )
        rows = conn.execute(
            f"""
            SELECT kf.period_end,
                   kf.value,
                   {unit_select},
                   kf.id AS fact_id,
                   kf.source_doc_id,
                   {locator_select},
                   {confidence_select},
                   {extracted_by_select},
                   {computed_from_select},
                   d.fetched_at,
                   d.source_url,
                   d.doc_type,
                   {accession_select},
                   {tier_select}
            FROM kpi_facts kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            JOIN documents d ON d.id = kf.source_doc_id
            WHERE kf.ticker = ?
              AND kd.name = ?
              AND kf.fiscal_period_type IN ({placeholders})
              AND kf.id = (
                SELECT kf2.id
                FROM kpi_facts kf2
                JOIN documents d2 ON d2.id = kf2.source_doc_id
                WHERE kf2.ticker = kf.ticker
                  AND kf2.kpi_definition_id = kf.kpi_definition_id
                  AND kf2.period_end = kf.period_end
                  AND kf2.fiscal_period_type = kf.fiscal_period_type
                ORDER BY {rank_expr.replace("d.", "d2.")} DESC, kf2.id DESC
                LIMIT 1
              )
            ORDER BY kf.period_end ASC
            """,
            (ticker.upper(), kpi_name, *period_list),
        ).fetchall()
        return _overlay_sourced_series(
            conn,
            ticker=ticker,
            fact_kind=KPI,
            fact_key=kpi_name,
            period_types=period_list,
            series=_sourced_rows(rows, fact_table="kpi_facts"),
        )
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "timeseries_load_kpi_sourced_failed",
                "ticker": ticker,
                "kpi": kpi_name,
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
            return _overlay_scalar_series(
                conn,
                ticker=ticker,
                fact_kind=KPI,
                fact_key=kpi_name,
                period_types=period_list,
                series=_rows_to_series(rows),
            )

        has_tier = _has_column(conn, "documents", "source_quality_tier")
        rank_expr = _tier_rank_case_sql("d.source_quality_tier") if has_tier else "0"
        as_of_clause = "AND d.fetched_at <= ? " if as_of_cutoff is not None else ""
        as_of_params: tuple[object, ...] = (as_of_cutoff,) if as_of_cutoff is not None else ()
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
        return _overlay_scalar_series(
            conn,
            ticker=ticker,
            fact_kind=KPI,
            fact_key=kpi_name,
            period_types=period_list,
            series=_rows_to_series(rows),
        )
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


def load_segment_junction_series(
    ticker: str,
    dims: list[tuple[str, str]],
    metric: str,
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    period_types: Iterable[str] = DEFAULT_PERIOD_TYPES,
    as_of_date: date | datetime | str | None = None,
) -> list[Observation]:
    """Load a chronological series from segment_periods + segment_dimensions.

    `dims` is a list of (dim_type, dim_name) constraints. With one entry the
    loader returns a simple 1-D series (mirrors load_segment_series). With
    multiple entries the loader self-joins segment_dimensions so the returned
    periods carry a dim row matching EACH constraint — the AND semantics
    needed for cross-tab queries like ``[("product","AWS"), ("geography","US")]``.

    The numeric value is taken from the FIRST dim filter (i.e. the leading
    axis of the requested cross-tab). Across multiple segment_periods rows
    for the same (ticker, period_end, fiscal_period_type) — which happens
    when an SEC-extracted period and an FMP-extracted period both target the
    same quarter — the loader prefers the period whose source document has
    the higher source_quality_tier (sec_official > fmp_normalized > ...).
    Ties broken by max(sp.id) so the most recently ingested period wins.
    Within the chosen period, max(sd.id) wins for the dim row.

    On synthetic test fixtures missing documents.source_quality_tier (or
    documents entirely), the loader falls back to max(sp.id) only.
    """
    if not dims:
        return []
    resolved = _resolve_db_path(repo_root, db_path)
    if resolved is None:
        return []
    conn = _open(resolved)
    if conn is None:
        return []
    period_list = list(period_types) or list(DEFAULT_PERIOD_TYPES)
    period_placeholders = ",".join("?" * len(period_list))
    as_of_cutoff = _normalize_as_of(as_of_date)
    try:
        for required in ("segment_periods", "segment_dimensions"):
            if (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (required,),
                ).fetchone()
                is None
            ):
                return []

        head_dim_type, head_dim_name = dims[0]
        extra_dim_joins: list[str] = []
        extra_dim_params: list[object] = []
        for idx, (dt, dn) in enumerate(dims[1:], start=2):
            alias = f"sd{idx}"
            extra_dim_joins.append(
                f"JOIN segment_dimensions {alias} "
                f"ON {alias}.period_id = sp.id "
                f"AND {alias}.dim_type = ? "
                f"AND {alias}.dim_name = ? "
                f"AND {alias}.metric = ?"
            )
            extra_dim_params.extend([dt, dn, metric])
        joins = "\n".join(extra_dim_joins)

        tier_aware = _has_table(conn, "documents") and _has_column(
            conn, "documents", "source_quality_tier"
        )
        if tier_aware:
            tier_case = _tier_rank_case_sql("d.source_quality_tier")
            as_of_clause = "AND d.fetched_at <= ?" if as_of_cutoff is not None else ""
            as_of_params: list[object] = [as_of_cutoff] if as_of_cutoff is not None else []
            sql = f"""
                WITH ranked_periods AS (
                    SELECT
                        sp_inner.id AS id,
                        ROW_NUMBER() OVER (
                            PARTITION BY sp_inner.ticker, sp_inner.period_end,
                                         sp_inner.fiscal_period_type
                            ORDER BY {tier_case} DESC, sp_inner.id DESC
                        ) AS rn
                    FROM segment_periods sp_inner
                    LEFT JOIN documents d ON d.id = sp_inner.source_doc_id
                    WHERE sp_inner.ticker = ?
                      AND sp_inner.fiscal_period_type IN ({period_placeholders})
                      {as_of_clause}
                )
                SELECT sp.period_end, sd1.value
                FROM segment_periods sp
                JOIN ranked_periods rp ON rp.id = sp.id AND rp.rn = 1
                JOIN segment_dimensions sd1
                  ON sd1.period_id = sp.id
                  AND sd1.dim_type = ?
                  AND sd1.dim_name = ?
                  AND sd1.metric = ?
                {joins}
                WHERE sd1.id = (
                    SELECT MAX(sd1b.id) FROM segment_dimensions sd1b
                    WHERE sd1b.period_id = sp.id
                      AND sd1b.dim_type = sd1.dim_type
                      AND sd1b.dim_name = sd1.dim_name
                      AND sd1b.metric = sd1.metric
                )
                ORDER BY sp.period_end ASC
            """
            params: list[object] = [
                ticker.upper(),
                *period_list,
                *as_of_params,
                head_dim_type,
                head_dim_name,
                metric,
                *extra_dim_params,
            ]
        else:
            sql = f"""
                SELECT sp.period_end, sd1.value
                FROM segment_periods sp
                JOIN segment_dimensions sd1
                  ON sd1.period_id = sp.id
                  AND sd1.dim_type = ?
                  AND sd1.dim_name = ?
                  AND sd1.metric = ?
                {joins}
                WHERE sp.ticker = ?
                  AND sp.fiscal_period_type IN ({period_placeholders})
                  AND sd1.id = (
                    SELECT MAX(sd1b.id) FROM segment_dimensions sd1b
                    WHERE sd1b.period_id = sp.id
                      AND sd1b.dim_type = sd1.dim_type
                      AND sd1b.dim_name = sd1.dim_name
                      AND sd1b.metric = sd1.metric
                  )
                ORDER BY sp.period_end ASC
            """
            params = [
                head_dim_type,
                head_dim_name,
                metric,
                *extra_dim_params,
                ticker.upper(),
                *period_list,
            ]
        rows = conn.execute(sql, params).fetchall()
        return _rows_to_series(rows)
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "timeseries_load_segment_junction_failed",
                "ticker": ticker,
                "dims": dims,
                "metric": metric,
                "error": str(exc),
            }
        )
        return []
    finally:
        conn.close()


def _segment_sourced_rows(rows: Iterable[sqlite3.Row]) -> list[SourcedObservation]:
    """Junction-provenance rows → ascending SourcedObservation list.

    Mirrors :func:`_sourced_rows` but for the segment columns: provenance is
    period-level (the winning ``segment_periods`` row's source document), so
    fact-level fields the junction has no analogue for (``fact_id``,
    ``locator``, ``confidence``, ``extracted_by``, ``computed_from``) are
    surfaced as None. Last row wins per period_end (the SQL already chose the
    canonical period via tier/id ordering)."""
    by_period: dict[datetime, SourcedObservation] = {}
    for r in rows:
        pe = _parse_period_end(r["period_end"])
        if pe is None:
            continue
        try:
            v = float(r["value"])
        except (TypeError, ValueError):
            continue
        src_doc = r["source_doc_id"]
        prov: dict[str, object] = {
            "source": str(r["source"]) if r["source"] is not None else "unknown",
            "fact_id": None,
            "source_doc_id": int(src_doc) if src_doc is not None else None,
            "fetched_at": str(r["fetched_at"]) if r["fetched_at"] is not None else None,
            "source_url": str(r["source_url"]) if r["source_url"] is not None else None,
            "doc_type": str(r["doc_type"]) if r["doc_type"] is not None else None,
            "accession_number": (
                str(r["accession_number"]) if r["accession_number"] is not None else None
            ),
            "filing_date": str(r["filing_date"]) if r["filing_date"] is not None else None,
            "locator": None,
            "confidence": None,
            "extracted_by": None,
            "computed_from": None,
        }
        unit_raw = r["unit"]
        by_period[pe] = SourcedObservation(
            period_end=pe,
            value=v,
            unit=str(unit_raw) if unit_raw is not None else None,
            provenance=prov,
        )
    return [by_period[pe] for pe in sorted(by_period)]


def load_segment_junction_series_with_provenance(
    ticker: str,
    dims: list[tuple[str, str]],
    metric: str,
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    period_types: Iterable[str] = DEFAULT_PERIOD_TYPES,
    as_of_date: date | datetime | str | None = None,
) -> list[SourcedObservation]:
    """`load_segment_junction_series` + per-period document provenance.

    The segment sibling of the fin/kpi ``_with_provenance`` loaders, so the
    ViewSpec engine (master build P5.1) can chip segment cells like every
    other cell. Segment provenance is period-level: the junction joins
    ``documents`` through ``segment_periods.source_doc_id``, so the chip names
    the document the winning period was extracted from. Same tier+id period
    pick as :func:`load_segment_junction_series`.

    Best-effort and never drops values for want of provenance: a missing
    ``documents`` table yields the same value series with ``source`` =
    'unknown' rather than []. Returns [] only when the segment tables / DB are
    missing (matching the value loader)."""
    if not dims:
        return []
    resolved = _resolve_db_path(repo_root, db_path)
    if resolved is None:
        return []
    conn = _open(resolved)
    if conn is None:
        return []
    period_list = list(period_types) or list(DEFAULT_PERIOD_TYPES)
    period_placeholders = ",".join("?" * len(period_list))
    as_of_cutoff = _normalize_as_of(as_of_date)
    try:
        for required in ("segment_periods", "segment_dimensions"):
            if not _has_table(conn, required):
                return []

        head_dim_type, head_dim_name = dims[0]
        extra_dim_joins: list[str] = []
        extra_dim_params: list[object] = []
        for idx, (dt, dn) in enumerate(dims[1:], start=2):
            alias = f"sd{idx}"
            extra_dim_joins.append(
                f"JOIN segment_dimensions {alias} "
                f"ON {alias}.period_id = sp.id "
                f"AND {alias}.dim_type = ? "
                f"AND {alias}.dim_name = ? "
                f"AND {alias}.metric = ?"
            )
            extra_dim_params.extend([dt, dn, metric])
        joins = "\n".join(extra_dim_joins)

        has_docs = _has_table(conn, "documents")
        has_tier = has_docs and _has_column(conn, "documents", "source_quality_tier")
        if has_docs:
            tier_select = (
                "d.source_quality_tier AS source"
                if has_tier
                else "COALESCE(d.source_type, 'unknown') AS source"
            )
            accession_select = (
                "d.accession_number, d.filing_date"
                if _has_column(conn, "documents", "accession_number")
                else "NULL AS accession_number, NULL AS filing_date"
            )
            prov_cols = f"d.fetched_at, d.source_url, d.doc_type, {accession_select}, {tier_select}"
            doc_join = "LEFT JOIN documents d ON d.id = sp.source_doc_id"
        else:
            prov_cols = (
                "NULL AS fetched_at, NULL AS source_url, NULL AS doc_type, "
                "NULL AS accession_number, NULL AS filing_date, 'unknown' AS source"
            )
            doc_join = ""

        dim_value_clause = f"""
            JOIN segment_dimensions sd1
              ON sd1.period_id = sp.id
              AND sd1.dim_type = ?
              AND sd1.dim_name = ?
              AND sd1.metric = ?
            {joins}
            WHERE sd1.id = (
                SELECT MAX(sd1b.id) FROM segment_dimensions sd1b
                WHERE sd1b.period_id = sp.id
                  AND sd1b.dim_type = sd1.dim_type
                  AND sd1b.dim_name = sd1.dim_name
                  AND sd1b.metric = sd1.metric
            )
        """
        dim_params: list[object] = [head_dim_type, head_dim_name, metric, *extra_dim_params]

        if has_tier:
            tier_case = _tier_rank_case_sql("d.source_quality_tier")
            as_of_clause = "AND d.fetched_at <= ?" if as_of_cutoff is not None else ""
            as_of_params: list[object] = [as_of_cutoff] if as_of_cutoff is not None else []
            sql = f"""
                WITH ranked_periods AS (
                    SELECT
                        sp_inner.id AS id,
                        ROW_NUMBER() OVER (
                            PARTITION BY sp_inner.ticker, sp_inner.period_end,
                                         sp_inner.fiscal_period_type
                            ORDER BY {tier_case} DESC, sp_inner.id DESC
                        ) AS rn
                    FROM segment_periods sp_inner
                    LEFT JOIN documents d ON d.id = sp_inner.source_doc_id
                    WHERE sp_inner.ticker = ?
                      AND sp_inner.fiscal_period_type IN ({period_placeholders})
                      {as_of_clause}
                )
                SELECT sp.period_end, sd1.value, sp.unit, sp.source_doc_id, {prov_cols}
                FROM segment_periods sp
                JOIN ranked_periods rp ON rp.id = sp.id AND rp.rn = 1
                {doc_join}
                {dim_value_clause}
                ORDER BY sp.period_end ASC
            """
            params: list[object] = [ticker.upper(), *period_list, *as_of_params, *dim_params]
        else:
            sql = f"""
                SELECT sp.period_end, sd1.value, sp.unit, sp.source_doc_id, {prov_cols}
                FROM segment_periods sp
                {doc_join}
                {dim_value_clause}
                  AND sp.ticker = ?
                  AND sp.fiscal_period_type IN ({period_placeholders})
                ORDER BY sp.period_end ASC
            """
            params = [*dim_params, ticker.upper(), *period_list]
        rows = conn.execute(sql, params).fetchall()
        return _segment_sourced_rows(rows)
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "timeseries_load_segment_junction_prov_failed",
                "ticker": ticker,
                "dims": dims,
                "metric": metric,
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

    Thin compatibility shim over `load_segment_junction_series`. Callers still
    speak the legacy `segment_facts` vocabulary (a single `segment_name` plus
    one of `revenue_by_product` / `revenue_by_geography` / `operating_income`);
    this function translates that pair into the junction's
    (dim_type, dim_name, metric) form via the canonical mapping in
    `pipeline.segment_junction_writer` and delegates.

    `as_of_date` is accepted for API parity with the other loaders but
    currently has no effect — segment_periods does not yet carry the
    documents.fetched_at FK chain the financial/kpi loaders use for
    time-travel. Wire it up when audit-trail coverage extends to segments.
    """
    # Local import keeps the loaders module free of a write-side dependency
    # at import time — the writer module pulls in pydantic models we don't
    # otherwise need to evaluate just to load a series.
    from pipeline.segment_junction_writer import (
        _LEGACY_METRIC_TO_DIM_TYPE,
        _LEGACY_METRIC_TO_JUNCTION_METRIC,
    )

    dim_type_enum = _LEGACY_METRIC_TO_DIM_TYPE.get(metric)
    if dim_type_enum is None:
        # Unknown legacy metric: fall through with the original metric
        # string so the junction loader can still match documents that
        # carry a non-standard metric verbatim (mirrors
        # segment_fact_to_dimension's BUSINESS_UNIT fallback).
        dim_type = "business_unit"
        junction_metric = metric
    else:
        dim_type = dim_type_enum.value
        junction_metric = _LEGACY_METRIC_TO_JUNCTION_METRIC.get(metric, metric)

    return load_segment_junction_series(
        ticker,
        dims=[(dim_type, segment_name)],
        metric=junction_metric,
        repo_root=repo_root,
        db_path=db_path,
        period_types=period_types,
        as_of_date=as_of_date,
    )
