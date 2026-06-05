"""Backfill NU's "Activity Rate" + consolidated "NPL 15d+ total" KPI series.

Both metrics are needed for two `micro_thesis/holdings/NU.json` break rules that
otherwise evaluate UNRESOLVED:
  - `nu_activity_rate_below_82`  (business_model rule, reads the level directly)
  - `npl_total_yoy_deterioration` (reads the YoY-change transform of def "NPL 15d+
    ratio (consolidated total ...)", which only materializes once the base series
    has same-fiscal-quarter pairs — see src/compute/fmp_derived_kpis.py).

Neither value lives in FMP, the IR historical-data spreadsheet (no activity-rate
row; its NPL rows are the Nu-Holdings *consolidated* series, not what NU
headlines), nor reliably in the `.tmp` synthesized briefs. They are read from NU's
quarterly IR presentation decks (`ir_documents/NU/<period>/ir_presentation__*.pdf`,
text layer) and persisted here at IR_DOC tier via the canonical
`pipeline.kpi_persistence.persist_manifest` path, attached per quarter to that
quarter's `ir_presentation_synthesized` brief document (the same provenance
family as the pre-existing def-637 prints).

Scope notes (NU's own disclosure evolution):
  * "Activity Rate" = monthly active customers / total customers (NU's slide-note
    definition). Values are the global figure from the activity slide / cover.
  * The consolidated "NPL 15d+ total" = as-presented 15-90d + 90+ NPL. Through
    Q3'25 NU headlined the "Brazil Consumer Credit Portfolio" delinquency slide;
    the redesigned Q4'25 deck switched the headline NPL to "across all
    geographies" (consolidated), so Q4'25 (10.7) uses that. Brazil vs consolidated
    differ <=0.2pp over this window, so the mixed-scope YoY is immaterial against
    the rule's 1.5pp threshold.
  * Q2'25 = 11.0 (Brazil 15-90 4.4% + 90+ 6.6%). An earlier brief extraction
    mis-read this as 11.3 (it grabbed Q1'25's 4.7% for the 15-90 leg); this script
    repairs that stale row if present and clears the derived series so a
    subsequent `derive_kpis_from_fmp.py` rebuilds the YoY transform.

Idempotent: persist_manifest replays as a no-op on matching provenance, and the
Q2'25 repair only fires when a non-11.0 row is actually present.

After running, refresh the derived transform + verdict:
    python execution/derive_kpis_from_fmp.py  --ticker NU --db <db>
    python execution/run_thesis_evaluator.py  --ticker NU --db <db>

Usage:
    python scratch/backfill_nu_npl_activity_kpis.py
    python scratch/backfill_nu_npl_activity_kpis.py --db /path/to/portfolio.db --dry-run
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

from models.documents import SourceType  # noqa: E402
from models.facts import FiscalPeriodType, Unit  # noqa: E402
from pipeline.kpi_persistence import (  # noqa: E402
    KpiExtractionManifest,
    KpiValue,
    persist_manifest,
)
from pipeline.run_accounting import start_run  # noqa: E402

ACTIVITY_RATE = "Activity Rate"
NPL_TOTAL = "NPL 15d+ ratio (consolidated total: early-stage 15-90d + severe 90d+, YoY-tracked)"
DERIVED_NPL_YOY = "NPL 15d+ total YoY change (pp)"
EXTRACTED_BY = "ir_presentation_readout"

# (period_end, fiscal_quarter, ir_presentation_synthesized doc_id, activity_rate, npl_total)
# Values read from each quarter's IR presentation deck; cross-validated across
# adjacent decks' overlapping history. None = metric not added for that quarter.
ROWS: tuple[tuple[str, str, int, str | None, str | None], ...] = (
    ("2023-06-30", "Q2", 6841, "82.2", None),
    ("2023-09-30", "Q3", 6842, "82.8", None),
    ("2023-12-31", "Q4", 6843, "83.1", None),
    ("2024-03-31", "Q1", 6844, "83.2", None),
    ("2024-06-30", "Q2", 6845, "83.4", "11.5"),
    ("2024-09-30", "Q3", 6846, "83.6", "11.6"),
    ("2024-12-31", "Q4", 6847, "83.1", "11.1"),
    ("2025-03-31", "Q1", 6848, "83.2", "11.2"),
    ("2025-06-30", "Q2", 6849, None, "11.0"),  # NPL corrects a prior 11.3 mis-extraction
    ("2025-12-31", "Q4", 6851, "83.0", "10.7"),  # Q4'25 NPL is consolidated (deck switched scope)
)


def _repair_stale_q225_npl(conn: sqlite3.Connection, *, dry_run: bool) -> bool:
    """Remove a stale def-637 Q2'25 NPL row whose value isn't 11.0 (the known
    11.3 brief mis-extraction). Returns True if anything was deleted."""
    npl_def = conn.execute(
        "SELECT id FROM kpi_definitions WHERE ticker='NU' AND name=?", (NPL_TOTAL,)
    ).fetchone()
    if npl_def is None:
        return False
    stale = conn.execute(
        "SELECT value, source_doc_id FROM kpi_facts WHERE ticker='NU' "
        "AND kpi_definition_id=? AND period_end LIKE '2025-06-30%' "
        "AND CAST(value AS REAL) <> 11.0",
        (npl_def["id"],),
    ).fetchall()
    if not stale:
        return False
    print(
        f"  repair: stale Q2'25 NPL row(s) {[(str(s['value']), s['source_doc_id']) for s in stale]}"
    )
    if not dry_run:
        conn.execute(
            "DELETE FROM kpi_facts WHERE ticker='NU' AND kpi_definition_id=? "
            "AND period_end LIKE '2025-06-30%' AND CAST(value AS REAL) <> 11.0",
            (npl_def["id"],),
        )
        # Clear the derived YoY series so a subsequent derive rebuilds it from the
        # corrected base (the deriver would otherwise replay the same source_doc_id
        # and leave the stale YoY value in place).
        der = conn.execute(
            "SELECT id FROM kpi_definitions WHERE ticker='NU' AND name=?", (DERIVED_NPL_YOY,)
        ).fetchone()
        if der is not None:
            n = conn.execute(
                "DELETE FROM kpi_facts WHERE kpi_definition_id=?", (der["id"],)
            ).rowcount
            print(f"  repair: cleared {n} derived '{DERIVED_NPL_YOY}' rows (rebuild via derive)")
    return True


def backfill(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    repaired = _repair_stale_q225_npl(conn, dry_run=dry_run)
    total_inserted = 0
    run_id = start_run(conn, directive="backfill_nu_npl_activity_kpis", ticker_scope=["NU"])
    for period_end, q, doc_id, activity, npl in ROWS:
        values: list[KpiValue] = []
        if activity is not None:
            values.append(
                KpiValue(
                    name=ACTIVITY_RATE, value=Decimal(activity), unit=Unit.PERCENT, confidence=0.95
                )
            )
        if npl is not None:
            values.append(
                KpiValue(name=NPL_TOTAL, value=Decimal(npl), unit=Unit.PERCENT, confidence=0.9)
            )
        manifest = KpiExtractionManifest(
            ticker="NU",
            period_end=datetime.fromisoformat(period_end),
            fiscal_period_type=FiscalPeriodType(q),
            source_doc_id=doc_id,
            primary_source=SourceType.IR_DOC,
            extracted_by=EXTRACTED_BY,
            values=values,
        )
        if dry_run:
            print(
                f"  [dry-run] {period_end} {q}: would persist {[(v.name.split('(')[0].strip(), str(v.value)) for v in values]}"
            )
            continue
        res = persist_manifest(conn, run_id=run_id, manifest=manifest)
        total_inserted += res.inserted
        print(
            f"  {period_end} {q}: inserted={res.inserted} skipped={res.skipped_existing} issues={res.validation_issues}"
        )
    if not dry_run:
        conn.commit()
    print(f"repaired_q225={repaired}  total_inserted={total_inserted}")
    return total_inserted


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    p.add_argument("--dry-run", action="store_true", help="Print what would change without writing")
    args = p.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[error] no DB at {db_path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        backfill(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
