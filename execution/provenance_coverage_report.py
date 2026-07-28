"""Provenance coverage audit -- %-of-facts-with-renderable-provenance per
extractor per table (docs/design/provenance_clickthrough.md section 4.3).

Modeled on execution/capture_coverage_report.py's summary-print pattern, but
reads directly off the live financial_facts/kpi_facts/fact_overrides tables
(read-only, mode=ro) rather than a coverage log -- the locator column IS the
ground truth for whether a fact's provenance is click-through-able today. "v2
renderable" means locator IS NOT NULL, locator_version >= 2, and kind IS NOT
NULL -- i.e. FactLocator.effective_kind() resolves and the peek dispatcher
(pipeline.peeks.render_fact_provenance_peek) has something to render.

``fact_overrides`` (Phase C, alembic 0181) joined the audit the moment it
gained its own ``locator`` column -- same shape/predicate, no special-casing
needed, since it carries ``ticker``/``locator`` directly like the other two.
``segment_dimensions`` is NOT included: its ``ticker`` lives one join away on
``segment_periods``, so this generic single-table query shape doesn't fit it
without a rework -- deferred rather than special-cased here.

Usage:
    python execution/provenance_coverage_report.py
    python execution/provenance_coverage_report.py --ticker NU
    python execution/provenance_coverage_report.py --table financial_facts
    python execution/provenance_coverage_report.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_TABLES: tuple[str, ...] = ("financial_facts", "kpi_facts", "fact_overrides")


@dataclass(slots=True)
class ExtractorCoverage:
    """One extracted_by group's row count + v2-renderable count within a table."""

    extracted_by: str
    rows: int
    v2_renderable: int

    @property
    def pct(self) -> float:
        return round(self.v2_renderable / self.rows * 100, 1) if self.rows else 0.0


@dataclass(slots=True)
class TableCoverage:
    """One fact table's coverage rollup, plus its per-extractor breakdown."""

    table: str
    rows: int
    locator_null: int
    v1_only: int
    v2_renderable: int
    by_extractor: list[ExtractorCoverage] = field(default_factory=list["ExtractorCoverage"])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, table: str, conds: list[str], params: list[object]) -> int:
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    row = conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()
    return int(row[0]) if row is not None else 0


# The v2-renderable predicate: a locator present, stamped locator_version>=2,
# and carrying a discriminant kind -- exactly what FactLocator.effective_kind()
# needs to dispatch a peek render (see pipeline.peeks / models.facts).
_V2_RENDERABLE_SQL = (
    "locator IS NOT NULL "
    "AND json_extract(locator, '$.locator_version') >= 2 "
    "AND json_extract(locator, '$.kind') IS NOT NULL"
)


#: Which column carries the "who/how produced this row" tag, per table --
#: financial_facts/kpi_facts use `extracted_by`; fact_overrides (a company-doc
#: override, not an extraction) uses `created_by` for the same purpose
#: (e.g. "agent:extract_8k_overrides").
_EXTRACTOR_COLUMN: dict[str, str] = {
    "financial_facts": "extracted_by",
    "kpi_facts": "extracted_by",
    "fact_overrides": "created_by",
}


def audit_table(
    conn: sqlite3.Connection, table: str, *, ticker: str | None = None
) -> TableCoverage | None:
    """Coverage rollup for one table; None when the table doesn't exist on
    this DB (a pre-migration or minimal test substrate)."""
    if not _table_exists(conn, table):
        return None
    base_conds: list[str] = []
    params: list[object] = []
    if ticker:
        base_conds.append("UPPER(ticker) = ?")
        params.append(ticker.upper())

    total = _count(conn, table, base_conds, params)
    if total == 0:
        return TableCoverage(table=table, rows=0, locator_null=0, v1_only=0, v2_renderable=0)

    locator_null = _count(conn, table, [*base_conds, "locator IS NULL"], params)
    v2_renderable = _count(conn, table, [*base_conds, _V2_RENDERABLE_SQL], params)
    v1_only = max(total - locator_null - v2_renderable, 0)

    extractor_col = _EXTRACTOR_COLUMN.get(table, "extracted_by")
    eb_where = f"WHERE {' AND '.join([*base_conds, f'{extractor_col} IS NOT NULL'])}"
    rows = conn.execute(
        f"SELECT {extractor_col}, COUNT(*), "
        f"SUM(CASE WHEN {_V2_RENDERABLE_SQL} THEN 1 ELSE 0 END) "
        f"FROM {table} {eb_where} GROUP BY {extractor_col} ORDER BY 2 DESC",
        params,
    ).fetchall()
    by_extractor = [
        ExtractorCoverage(extracted_by=str(r[0]), rows=int(r[1]), v2_renderable=int(r[2] or 0))
        for r in rows
    ]
    return TableCoverage(
        table=table,
        rows=total,
        locator_null=locator_null,
        v1_only=v1_only,
        v2_renderable=v2_renderable,
        by_extractor=by_extractor,
    )


def _pct(n: int, total: int) -> float:
    return round(n / total * 100, 1) if total else 0.0


def format_report(coverages: list[TableCoverage]) -> str:
    lines: list[str] = []
    for c in coverages:
        lines.append(
            f"{c.table:<20}: {c.rows:>8,} rows | "
            f"locator NULL: {c.locator_null:,} ({_pct(c.locator_null, c.rows)}%) | "
            f"v1-only: {c.v1_only:,} ({_pct(c.v1_only, c.rows)}%) | "
            f"v2 renderable: {c.v2_renderable:,} ({_pct(c.v2_renderable, c.rows)}%)"
        )
        if c.by_extractor:
            lines.append("  by extracted_by:")
            for e in c.by_extractor:
                lines.append(
                    f"    {e.extracted_by:<20}: {e.rows:>8,} rows | "
                    f"v2 renderable: {e.v2_renderable:,} ({e.pct}%)"
                )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=PROJECT_ROOT / "data" / "portfolio.db")
    parser.add_argument("--ticker", help="Restrict the audit to one ticker.")
    parser.add_argument("--table", choices=_TABLES, help="Restrict to one table (default: both).")
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of text."
    )
    args = parser.parse_args()

    if not args.db_path.exists():
        print(
            json.dumps({"event": "db_not_found", "path": str(args.db_path)}),
            file=sys.stderr,
        )
        return 1

    tables = [args.table] if args.table else list(_TABLES)
    conn = connect_sqlite(args.db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        coverages = [
            cov for t in tables if (cov := audit_table(conn, t, ticker=args.ticker)) is not None
        ]
    finally:
        conn.close()

    if not coverages:
        print("No matching tables found.")
        return 0

    if args.json:
        print(json.dumps([asdict(c) for c in coverages], indent=2))
    else:
        print(format_report(coverages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
