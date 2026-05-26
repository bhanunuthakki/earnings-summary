"""Backfill segment_facts into the new segment_periods + segment_dimensions tables.

Reads every row from `segment_facts`, groups by
(ticker, period_end, fiscal_period_type, source_doc_id, currency, unit), and
writes each group as one segment_periods row plus one segment_dimensions row
per legacy fact. Idempotent: re-running on an already-backfilled DB is a no-op
(the writer dedupes by the natural key on both sides).

Usage:
    python scratch/backfill_segment_junction.py
    python scratch/backfill_segment_junction.py --db /path/to/portfolio.db
    python scratch/backfill_segment_junction.py --ticker AMZN --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.facts import (  # noqa: E402
    Currency,
    FiscalPeriodType,
    SegmentDimension,
    Unit,
)
from pipeline.segment_junction_writer import (  # noqa: E402
    segment_fact_to_dimension,
    write_segment_facts_junction,
)

_GroupKey = tuple[str, str, str, int, str | None, str]


def _open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found at {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _parse_period_end(raw: str | datetime) -> datetime:
    """sqlite returns DATETIME as text — tolerate the common shapes."""
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.strptime(s[:10], "%Y-%m-%d")


def _load_segment_facts(
    conn: sqlite3.Connection, ticker: str | None
) -> dict[_GroupKey, list[tuple[str, str, Decimal]]]:
    """Group legacy facts by (ticker, period, period_type, source_doc, currency, unit).

    Returns a dict mapping group key → list of (segment_name, metric, value).
    The grouping mirrors segment_periods' uniqueness shape, with currency / unit
    folded in so a period row can carry exactly one currency+unit pairing.
    """
    where = ""
    params: tuple[object, ...] = ()
    if ticker is not None:
        where = "WHERE ticker = ?"
        params = (ticker.upper(),)

    rows = conn.execute(
        f"""
        SELECT ticker, period_end, fiscal_period_type, source_doc_id,
               currency, unit, segment_name, metric, value
        FROM segment_facts
        {where}
        """,
        params,
    ).fetchall()

    groups: dict[_GroupKey, list[tuple[str, str, Decimal]]] = defaultdict(list)
    for r in rows:
        key: _GroupKey = (
            str(r["ticker"]).upper(),
            str(r["period_end"]),
            str(r["fiscal_period_type"]),
            int(r["source_doc_id"]),
            (str(r["currency"]) if r["currency"] is not None else None),
            str(r["unit"]),
        )
        groups[key].append(
            (
                str(r["segment_name"]),
                str(r["metric"]),
                Decimal(str(r["value"])),
            )
        )
    return groups


def _coerce_currency(raw: str | None) -> Currency | None:
    if raw is None:
        return None
    try:
        return Currency(raw)
    except ValueError:
        return None


def _coerce_unit(raw: str) -> Unit:
    try:
        return Unit(raw)
    except ValueError:
        return Unit.ACTUAL


def _coerce_period_type(raw: str) -> FiscalPeriodType | None:
    try:
        return FiscalPeriodType(raw)
    except ValueError:
        return None


def backfill(
    conn: sqlite3.Connection, *, ticker: str | None, dry_run: bool
) -> tuple[int, int, int]:
    """Run the backfill; return (groups, periods_inserted, dimensions_inserted)."""
    groups = _load_segment_facts(conn, ticker)

    periods_inserted = 0
    dimensions_inserted = 0
    skipped_period_types = 0

    for (key_ticker, period_end_raw, period_type_raw, source_doc_id, currency_raw, unit_raw), facts in groups.items():
        period_type = _coerce_period_type(period_type_raw)
        if period_type is None:
            # Legacy segment_facts rarely contains non-FiscalPeriodType strings
            # (e.g. "quarterly"); these don't fit the junction's enum so skip.
            skipped_period_types += 1
            continue
        period_end = _parse_period_end(period_end_raw)
        currency = _coerce_currency(currency_raw)
        unit = _coerce_unit(unit_raw)

        dimensions: list[SegmentDimension] = [
            segment_fact_to_dimension(segment_name, metric, value)
            for segment_name, metric, value in facts
        ]
        if not dimensions:
            continue
        if dry_run:
            periods_inserted += 1
            dimensions_inserted += len(dimensions)
            continue
        p_ins, d_ins = write_segment_facts_junction(
            conn,
            ticker=key_ticker,
            period_end=period_end,
            fiscal_period_type=period_type,
            source_doc_id=source_doc_id,
            currency=currency,
            unit=unit,
            dimensions=dimensions,
        )
        periods_inserted += p_ins
        dimensions_inserted += d_ins

    if not dry_run:
        conn.commit()

    if skipped_period_types:
        sys.stderr.write(
            f"warning: skipped {skipped_period_types} group(s) with "
            f"non-canonical fiscal_period_type — they don't match FiscalPeriodType enum\n"
        )

    return (len(groups), periods_inserted, dimensions_inserted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "data" / "portfolio.db"),
        help="Path to portfolio.db",
    )
    parser.add_argument("--ticker", help="Restrict backfill to a single ticker")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print row counts without writing to the DB",
    )
    args = parser.parse_args()

    conn = _open_db(Path(args.db))
    try:
        groups, periods, dimensions = backfill(
            conn, ticker=args.ticker, dry_run=args.dry_run
        )
    finally:
        conn.close()

    verb = "would insert" if args.dry_run else "inserted"
    print(
        f"backfill_segment_junction: {len(args.ticker) if args.ticker else 'all'} "
        f"tickers; {groups} group(s); {verb} {periods} period row(s) "
        f"and {dimensions} dimension row(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
