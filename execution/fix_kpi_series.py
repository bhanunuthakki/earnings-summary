"""execution/fix_kpi_series.py
-------------------------------
Single-purpose idempotent CLI for red-team PR2 item 4 (KPI cumulative-series
sanity guard — `directives/monthly_red_team.md` Phase 1 "KPI series sanity").

Scans one or more cumulative-marked KPI series (the same allowlist
`pipeline.kpi_persistence._is_cumulative_kpi` uses to guard NEW writes) for
two independent, already-persisted data-quality problems:

  1. UNIT ERROR — a value that is a >`--jump-ratio`x (default 1000x) outlier
     vs its chronological neighbor(s) AND exceeds `--unit-error-floor`
     (default 1e6). This is the exact NU "Total customers" def 641
     corruption the 2026-07 red-team audit found: a raw-count row
     (114,000,000) landing inside a millions-scale series (neighbors ~110-135).
     `--apply` divides the offending value by `--unit-scale` (default 1e6)
     and stamps a provenance note in `kpi_facts.source_excerpt` +
     `extracted_by`. `--dry-run` (the default) only reports it.
  2. NON-MONOTONIC — a value that decreases from its chronological
     predecessor. Listed for manual review only — NEVER auto-fixed. Which
     side is wrong (the higher or the lower print) isn't inferable from the
     series alone; guessing would be exactly the "guess-fix" the repo's
     schema-drift rule forbids.

Idempotent: re-running after `--apply` finds nothing left to fix for the rows
already corrected (the corrected value is no longer a >jump-ratio outlier),
so a second run is a clean no-op for that row.

Read the AGENTS.md schema-drift rule before extending this: never widen the
unit-error heuristic to "auto-correct" a non-monotonic row too — that
direction genuinely needs a human to look at the source document.

Usage:
    # Dry-run one named series (default — no writes, safe against any DB):
    python execution/fix_kpi_series.py --ticker NU --kpi-name "Total customers (millions)" --db data/portfolio.db

    # Dry-run every cumulative-marked series for one ticker:
    python execution/fix_kpi_series.py --ticker NU --db data/portfolio.db

    # Dry-run every cumulative-marked series across all tickers:
    python execution/fix_kpi_series.py --db data/portfolio.db

    # Apply the unit-error fixes (never against prod from an agent session —
    # the orchestrator's data pass runs --apply):
    python execution/fix_kpi_series.py --ticker NU --kpi-name "Total customers (millions)" --db data/portfolio.db --apply

    # Read-only against prod (safe: SQLite URI mode=ro refuses any write):
    python execution/fix_kpi_series.py --db "file:C:/path/to/portfolio.db?mode=ro" --uri
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pydantic import BaseModel, Field  # noqa: E402

from pipeline.kpi_persistence import (  # noqa: E402
    _decimal_unit_jump,  # pyright: ignore[reportPrivateUsage]
    _is_cumulative_kpi,  # pyright: ignore[reportPrivateUsage]
)
from sqlite_runtime import (  # noqa: E402
    SQLiteConnectionRole,
    connect_sqlite,
    require_safe_sqlite_writer_runtime,
)

_DEFAULT_UNIT_ERROR_FLOOR = Decimal("1e6")
_DEFAULT_UNIT_SCALE = Decimal("1e6")
_DEFAULT_JUMP_RATIO = Decimal("1000")


class KpiSeriesRow(BaseModel):
    """One kpi_facts row as read for scanning — typed, not a bare dict."""

    id: int
    period_end: str
    fiscal_period_type: str
    value: Decimal
    source_doc_id: int


class UnitErrorFinding(BaseModel):
    """One row flagged as a probable unit-scale error (candidate for --apply)."""

    fact_id: int
    ticker: str
    kpi_definition_id: int
    kpi_name: str
    period_end: str
    fiscal_period_type: str
    old_value: str
    proposed_value: str
    source_doc_id: int
    reason: str


class NonMonotonicFinding(BaseModel):
    """One row flagged as non-monotonic vs its chronological predecessor.

    Review-only — this script never guesses which side (this row or the
    prior one) is the wrong one.
    """

    fact_id: int
    ticker: str
    kpi_definition_id: int
    kpi_name: str
    period_end: str
    fiscal_period_type: str
    value: str
    prior_period_end: str
    prior_value: str


class FixKpiSeriesReport(BaseModel):
    db_path: str
    apply: bool
    scanned_definitions: int
    scanned_rows: int
    unit_error_findings: list[UnitErrorFinding] = Field(default_factory=list[UnitErrorFinding])
    non_monotonic_findings: list[NonMonotonicFinding] = Field(
        default_factory=list[NonMonotonicFinding]
    )
    applied: int = 0


def _cumulative_definitions(
    conn: sqlite3.Connection, *, ticker: str | None, kpi_name: str | None
) -> list[tuple[int, str, str]]:
    """Return [(id, ticker, name), ...] for kpi_definitions matching the
    cumulative-marker allowlist, optionally scoped to one ticker and/or one
    exact KPI name."""
    sql = "SELECT id, ticker, name FROM kpi_definitions WHERE 1=1"
    params: list[object] = []
    if ticker is not None:
        sql += " AND ticker = ?"
        params.append(ticker.upper())
    if kpi_name is not None:
        sql += " AND name = ?"
        params.append(kpi_name)
    sql += " ORDER BY ticker, name"
    out: list[tuple[int, str, str]] = []
    for row in conn.execute(sql, params).fetchall():
        if _is_cumulative_kpi(str(row["name"])):
            out.append((int(row["id"]), str(row["ticker"]), str(row["name"])))
    return out


def _series_rows(conn: sqlite3.Connection, kpi_definition_id: int) -> list[KpiSeriesRow]:
    cur = conn.execute(
        "SELECT id, period_end, fiscal_period_type, value, source_doc_id "
        "FROM kpi_facts WHERE kpi_definition_id = ? ORDER BY period_end ASC",
        (kpi_definition_id,),
    )
    out: list[KpiSeriesRow] = []
    for row in cur.fetchall():
        period_raw = row["period_end"]
        period_str = (
            period_raw.date().isoformat()
            if isinstance(period_raw, datetime)
            else str(period_raw)[:10]
        )
        out.append(
            KpiSeriesRow(
                id=int(row["id"]),
                period_end=period_str,
                fiscal_period_type=str(row["fiscal_period_type"]),
                value=Decimal(str(row["value"])),
                source_doc_id=int(row["source_doc_id"]),
            )
        )
    return out


def _scan_series(
    ticker: str,
    kpi_definition_id: int,
    kpi_name: str,
    rows: list[KpiSeriesRow],
    *,
    jump_ratio: Decimal,
    unit_error_floor: Decimal,
    unit_scale: Decimal,
) -> tuple[list[UnitErrorFinding], list[NonMonotonicFinding]]:
    """Classify each row against its chronological neighbor(s).

    A row is a UNIT_ERROR candidate when it exceeds `unit_error_floor` AND is
    a >`jump_ratio`x outlier vs at least one neighbor — matching the
    persist-time guard's heuristic (`pipeline.kpi_persistence._decimal_unit_jump`)
    so "what counts as a unit error" can't drift between the two call sites.
    A row that decreases from its immediate predecessor (and isn't already
    claimed as a unit-error row) is NON_MONOTONIC — review-only.
    """
    unit_errors: list[UnitErrorFinding] = []
    non_monotonic: list[NonMonotonicFinding] = []
    unit_error_ids: set[int] = set()

    for i, row in enumerate(rows):
        neighbors = [rows[i - 1]] if i > 0 else []
        if i + 1 < len(rows):
            neighbors.append(rows[i + 1])
        is_unit_error = row.value > unit_error_floor and any(
            _decimal_unit_jump(n.value, row.value, ratio_limit=jump_ratio) for n in neighbors
        )
        if is_unit_error:
            unit_error_ids.add(row.id)
            unit_errors.append(
                UnitErrorFinding(
                    fact_id=row.id,
                    ticker=ticker,
                    kpi_definition_id=kpi_definition_id,
                    kpi_name=kpi_name,
                    period_end=row.period_end,
                    fiscal_period_type=row.fiscal_period_type,
                    old_value=str(row.value),
                    proposed_value=str(row.value / unit_scale),
                    source_doc_id=row.source_doc_id,
                    reason=(
                        f">{jump_ratio}x jump vs neighbor(s) "
                        f"[{', '.join(str(n.value) for n in neighbors)}] "
                        f"and > floor {unit_error_floor}"
                    ),
                )
            )

    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        if cur.id in unit_error_ids or prev.id in unit_error_ids:
            continue  # already explained by a flagged unit-scale row
        if cur.value < prev.value:
            non_monotonic.append(
                NonMonotonicFinding(
                    fact_id=cur.id,
                    ticker=ticker,
                    kpi_definition_id=kpi_definition_id,
                    kpi_name=kpi_name,
                    period_end=cur.period_end,
                    fiscal_period_type=cur.fiscal_period_type,
                    value=str(cur.value),
                    prior_period_end=prev.period_end,
                    prior_value=str(prev.value),
                )
            )

    return unit_errors, non_monotonic


def _apply_unit_error_fixes(
    conn: sqlite3.Connection, findings: list[UnitErrorFinding], *, unit_scale: Decimal
) -> int:
    """Write the corrected values. Only ever touches rows in `findings`
    (unit-error candidates) — non-monotonic rows are never written here."""
    applied = 0
    now = datetime.now().isoformat(timespec="seconds")
    for f in findings:
        note = (
            f"fix_kpi_series.py: {f.old_value} -> {f.proposed_value} "
            f"(unit-scale /{unit_scale:g}) at {now}"
        )
        cur = conn.execute(
            "SELECT extracted_by, source_excerpt FROM kpi_facts WHERE id = ?", (f.fact_id,)
        )
        row = cur.fetchone()
        prior_extracted_by = (row["extracted_by"] if row is not None else None) or ""
        prior_excerpt = (row["source_excerpt"] if row is not None else None) or ""
        new_extracted_by = f"{prior_extracted_by}|fix:unit_scale"[:64]
        new_excerpt = f"{prior_excerpt} | {note}".strip(" |")[:1024]
        conn.execute(
            "UPDATE kpi_facts SET value = ?, extracted_by = ?, source_excerpt = ? WHERE id = ?",
            (str(f.proposed_value), new_extracted_by, new_excerpt, f.fact_id),
        )
        applied += 1
    conn.commit()
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=str, required=True, help="DB path, or a full sqlite3 URI when --uri is set."
    )
    parser.add_argument(
        "--uri",
        action="store_true",
        help="Treat --db as a full sqlite3 URI (e.g. 'file:...?mode=ro') instead of a bare path.",
    )
    parser.add_argument("--ticker", type=str, default=None, help="Restrict scan to one ticker.")
    parser.add_argument(
        "--kpi-name",
        type=str,
        default=None,
        help="Restrict scan to one exact kpi_definitions.name (requires --ticker).",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write the unit-error fixes. Default is dry-run."
    )
    parser.add_argument(
        "--jump-ratio",
        type=str,
        default=str(_DEFAULT_JUMP_RATIO),
        help=f"Outlier ratio vs a neighbor to flag a unit error (default {_DEFAULT_JUMP_RATIO}).",
    )
    parser.add_argument(
        "--unit-error-floor",
        type=str,
        default=str(_DEFAULT_UNIT_ERROR_FLOOR),
        help=f"Minimum magnitude to consider for a unit-error flag (default {_DEFAULT_UNIT_ERROR_FLOOR:g}).",
    )
    parser.add_argument(
        "--unit-scale",
        type=str,
        default=str(_DEFAULT_UNIT_SCALE),
        help=f"Divisor applied to a flagged unit-error value under --apply (default {_DEFAULT_UNIT_SCALE:g}).",
    )
    args = parser.parse_args(argv)

    if args.kpi_name is not None and args.ticker is None:
        parser.error("--kpi-name requires --ticker")

    jump_ratio = Decimal(args.jump_ratio)
    unit_error_floor = Decimal(args.unit_error_floor)
    unit_scale = Decimal(args.unit_scale)

    db_target = args.db if args.uri else str(Path(args.db))
    if not args.uri and not Path(args.db).exists():
        print(f"ERROR: no DB at {args.db}", file=sys.stderr)
        return 2
    if args.apply and args.uri and "mode=ro" in db_target:
        print("ERROR: --apply cannot run against a mode=ro URI.", file=sys.stderr)
        return 2
    if args.apply:
        require_safe_sqlite_writer_runtime()

    if args.uri:
        # Intentional isolated-recovery seam: operators may supply a complete
        # SQLite URI whose policy cannot be safely reconstructed from a path.
        conn = sqlite3.connect(db_target, uri=True)
    else:
        conn = connect_sqlite(
            Path(db_target),
            role=(SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY),
            schema_preflight=args.apply,
        )
    conn.row_factory = sqlite3.Row
    try:
        definitions = _cumulative_definitions(conn, ticker=args.ticker, kpi_name=args.kpi_name)
        scanned_rows = 0
        all_unit_errors: list[UnitErrorFinding] = []
        all_non_monotonic: list[NonMonotonicFinding] = []
        for kpi_definition_id, def_ticker, def_name in definitions:
            rows = _series_rows(conn, kpi_definition_id)
            scanned_rows += len(rows)
            unit_errors, non_monotonic = _scan_series(
                def_ticker,
                kpi_definition_id,
                def_name,
                rows,
                jump_ratio=jump_ratio,
                unit_error_floor=unit_error_floor,
                unit_scale=unit_scale,
            )
            all_unit_errors.extend(unit_errors)
            all_non_monotonic.extend(non_monotonic)

        applied = 0
        if args.apply and all_unit_errors:
            applied = _apply_unit_error_fixes(conn, all_unit_errors, unit_scale=unit_scale)

        report = FixKpiSeriesReport(
            db_path=db_target,
            apply=args.apply,
            scanned_definitions=len(definitions),
            scanned_rows=scanned_rows,
            unit_error_findings=all_unit_errors,
            non_monotonic_findings=all_non_monotonic,
            applied=applied,
        )
        print(report.model_dump_json(indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
