"""One-off: backfill the audit-trail columns added in migration 0053.

Migration 0053 adds two opportunistic backfills inside `upgrade()`:
    - `documents.source_quality_tier` from `documents.source_type`
    - `financial_facts.extracted_by` + `kpi_facts.extracted_by` from the
      joined `documents.source_type`

That works on a fresh `alembic upgrade head` run, but if a prior run
landed the schema without the inline backfill (or new rows have come in
since), this script makes the backfill idempotent and explicit. Run it
once after the migration applies; it is safe to re-run.

The `confidence` column on existing rows stays at the schema default of
1.0 — those rows came from deterministic FMP/SEC extractors, so 1.0 is
the right semantic value.

Run:
    python scratch/backfill_audit_columns.py
    python scratch/backfill_audit_columns.py --dry-run
    python scratch/backfill_audit_columns.py --db-path /custom/path.db

Delete this script once the next round of extractor wiring writes
`extracted_by` explicitly on every insert (currently tracked under the
[[project_deferred_followups]] memo).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Keep in sync with alembic/versions/0053_audit_columns.py::_BACKFILL_TIER_BY_SOURCE_TYPE.
_TIER_BY_SOURCE_TYPE: list[tuple[str, str]] = [
    ("sec_xbrl", "sec_official"),
    ("fmp", "fmp_normalized"),
    ("ir_doc", "fmp_normalized"),
    ("transcript_audio", "fmp_normalized"),
    ("manual_csv", "fmp_normalized"),
    ("manual_entry", "fmp_normalized"),
    ("llm_extracted", "llm_extracted"),
]


def backfill_documents_tier(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    """Set `source_quality_tier` on documents where it's still at the default
    but a more specific mapping exists. Returns rows that would update."""
    total = 0
    for source_type, tier in _TIER_BY_SOURCE_TYPE:
        if dry_run:
            cur = conn.execute(
                "SELECT COUNT(*) FROM documents "
                "WHERE source_type = ? AND source_quality_tier != ?",
                (source_type, tier),
            )
            row = cur.fetchone()
            n = int(row[0]) if row else 0
        else:
            cur = conn.execute(
                "UPDATE documents SET source_quality_tier = ? "
                "WHERE source_type = ? AND source_quality_tier != ?",
                (tier, source_type, tier),
            )
            n = cur.rowcount
        total += n
        print(f"  {source_type:>20s} -> {tier:<16s} : {n} rows")
    return total


def backfill_facts_extracted_by(
    conn: sqlite3.Connection, table: str, *, dry_run: bool
) -> int:
    """Set `extracted_by` on fact rows where it's NULL, using the joined
    documents.source_type."""
    if dry_run:
        cur = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE extracted_by IS NULL"
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    cur = conn.execute(
        f"UPDATE {table} SET extracted_by = "
        f"  (SELECT source_type FROM documents WHERE documents.id = {table}.source_doc_id) "
        f"WHERE extracted_by IS NULL"
    )
    return cur.rowcount


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        default=PROJECT_ROOT / "data" / "portfolio.db",
        help="Path to portfolio.db.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts without writing.",
    )
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"DB not found: {args.db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(args.db_path))
    try:
        print(f"Backfill mode: {'DRY-RUN' if args.dry_run else 'WRITE'}")
        print(f"DB: {args.db_path}")
        print()

        print("documents.source_quality_tier")
        doc_total = backfill_documents_tier(conn, dry_run=args.dry_run)
        print(f"  total: {doc_total} rows")
        print()

        print("financial_facts.extracted_by")
        ff_n = backfill_facts_extracted_by(
            conn, "financial_facts", dry_run=args.dry_run
        )
        print(f"  {ff_n} rows")
        print()

        print("kpi_facts.extracted_by")
        kf_n = backfill_facts_extracted_by(
            conn, "kpi_facts", dry_run=args.dry_run
        )
        print(f"  {kf_n} rows")

        if not args.dry_run:
            conn.commit()
            print("\nCommitted.")
        else:
            print("\nDry-run; no writes.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
