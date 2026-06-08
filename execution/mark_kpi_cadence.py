"""Mark a KPI definition's reporting cadence + convert its facts to that axis.

Annual-only metrics (bank Basel III capital ratios, other 20-F/10-K-only figures)
were historically shoehorned onto ``fiscal_period_type='Q4'``. This CLI is the
one-shot + onboarding tool that makes a definition first-class annual:

  1. sets ``kpi_definitions.reporting_cadence`` for every definition matching
     ``--kpi`` (exact name OR parenthetical-insensitive normalized name);
  2. (annual only) re-tags that definition's **fiscal-year-end** kpi_facts rows
     (``period_end`` = Dec-31, currently ``Q4``/``quarterly``) to ``FY`` so the
     annual reader (``ANNUAL_FACT_PERIOD_TYPES``) picks them up. Genuine interim
     prints (Q1/Q2/Q3) are left untouched — preserved, just not on the annual
     axis;
  3. optionally corrects the definition's ``unit`` (``--unit``) — e.g. NU's CAR
     definition was stamped ``ratio`` though every fact + the break rule are
     ``percent``.

Idempotent: re-running is a no-op once the cadence is set and the year-end rows
are already ``FY``. Dry-run by default; pass ``--apply`` to write.

Usage:
    python execution/mark_kpi_cadence.py --ticker NU \
        --kpi "Capital adequacy ratio" --cadence annual --unit percent --apply
    python execution/mark_kpi_cadence.py --ticker NU --kpi "Capital adequacy ratio" \
        --cadence annual --db data/portfolio.db   # dry-run preview
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.kpi_resolver import normalize_kpi_name  # noqa: E402
from models.facts import Unit  # noqa: E402
from models.kpis import ReportingCadence  # noqa: E402

# Year-end fact rows to promote to FY for an annual KPI: Dec-31 prints currently
# tagged as the quarterly Q4 bucket (or the SEC 'quarterly' bucket).
_YEAR_END_SUFFIX = "-12-31"
_QUARTERLY_YEAR_END_TYPES = ("Q4", "quarterly")


def _matching_definition_ids(conn: sqlite3.Connection, ticker: str, kpi: str) -> list[int]:
    """kpi_definitions.id for every row whose name == kpi or normalize-matches it."""
    want = normalize_kpi_name(kpi)
    ids: list[int] = []
    for row in conn.execute(
        "SELECT id, name FROM kpi_definitions WHERE ticker = ?", (ticker.upper(),)
    ):
        stored = str(row["name"])
        if stored == kpi or normalize_kpi_name(stored) == want:
            ids.append(int(row["id"]))
    return ids


def _fact_breakdown(conn: sqlite3.Connection, def_ids: list[int]) -> dict[str, int]:
    """Count kpi_facts rows per fiscal_period_type for the target definitions."""
    if not def_ids:
        return {}
    placeholders = ",".join("?" * len(def_ids))
    out: dict[str, int] = {}
    for row in conn.execute(
        f"SELECT fiscal_period_type AS fpt, COUNT(*) AS n FROM kpi_facts "
        f"WHERE kpi_definition_id IN ({placeholders}) GROUP BY fiscal_period_type",
        def_ids,
    ):
        out[str(row["fpt"])] = int(row["n"])
    return out


def _convert(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    kpi: str,
    cadence: ReportingCadence,
    unit: Unit | None,
    apply: bool,
) -> dict[str, object]:
    if not any(
        r["name"] == "reporting_cadence"
        for r in conn.execute("PRAGMA table_info(kpi_definitions)").fetchall()
    ):
        raise SystemExit(
            "kpi_definitions.reporting_cadence missing — run `alembic upgrade head` first"
        )
    def_ids = _matching_definition_ids(conn, ticker, kpi)
    if not def_ids:
        return {"ticker": ticker.upper(), "kpi": kpi, "status": "no_definition", "def_ids": []}

    before = _fact_breakdown(conn, def_ids)
    placeholders = ",".join("?" * len(def_ids))

    # Rows that WILL be retagged Q4/quarterly(Dec-31) -> FY (annual only).
    retag_count = 0
    if cadence is ReportingCadence.ANNUAL:
        retag_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM kpi_facts WHERE kpi_definition_id IN ({placeholders}) "
                f"AND fiscal_period_type IN ({','.join('?' * len(_QUARTERLY_YEAR_END_TYPES))}) "
                "AND substr(period_end, 1, 10) LIKE ?",
                (*def_ids, *_QUARTERLY_YEAR_END_TYPES, f"%{_YEAR_END_SUFFIX}"),
            ).fetchone()[0]
        )

    if apply:
        conn.execute(
            f"UPDATE kpi_definitions SET reporting_cadence = ? WHERE id IN ({placeholders})",
            (cadence.value, *def_ids),
        )
        if unit is not None:
            conn.execute(
                f"UPDATE kpi_definitions SET unit = ? WHERE id IN ({placeholders})",
                (unit.value, *def_ids),
            )
        if cadence is ReportingCadence.ANNUAL and retag_count:
            conn.execute(
                f"UPDATE kpi_facts SET fiscal_period_type = 'FY' "
                f"WHERE kpi_definition_id IN ({placeholders}) "
                f"AND fiscal_period_type IN ({','.join('?' * len(_QUARTERLY_YEAR_END_TYPES))}) "
                "AND substr(period_end, 1, 10) LIKE ?",
                (*def_ids, *_QUARTERLY_YEAR_END_TYPES, f"%{_YEAR_END_SUFFIX}"),
            )
        conn.commit()

    after = _fact_breakdown(conn, def_ids) if apply else before
    return {
        "ticker": ticker.upper(),
        "kpi": kpi,
        "def_ids": def_ids,
        "cadence": cadence.value,
        "unit_set": unit.value if unit is not None else None,
        "year_end_rows_retagged_to_FY": retag_count,
        "facts_before": before,
        "facts_after": after,
        "applied": apply,
        "status": "applied" if apply else "dry_run",
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", required=True)
    p.add_argument("--kpi", required=True, help="KPI name (exact or normalized match)")
    p.add_argument(
        "--cadence",
        default="annual",
        choices=[c.value for c in ReportingCadence],
        help="Reporting cadence to set (default: annual)",
    )
    p.add_argument(
        "--unit",
        default=None,
        choices=[u.value for u in Unit],
        help="Optionally correct the definition's unit (e.g. percent)",
    )
    p.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    p.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        result = _convert(
            conn,
            ticker=args.ticker,
            kpi=args.kpi,
            cadence=ReportingCadence(args.cadence),
            unit=Unit(args.unit) if args.unit else None,
            apply=args.apply,
        )
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0 if result["status"] != "no_definition" else 1


if __name__ == "__main__":
    raise SystemExit(main())
