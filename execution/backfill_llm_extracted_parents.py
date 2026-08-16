"""execution/backfill_llm_extracted_parents.py
------------------------------------------------
One-shot (re-runnable) backfill of ``documents.parent_document_id`` for
``source_type='llm_extracted'`` rows inserted before the write-path guard
(``pipeline.kpi_persistence.guard_llm_extracted_parent``, wired into
``compute/kpi_extract_summaries.py::_ensure_summary_document_row``) existed.

Directive: directives/data_provenance.md §2 — "LLM-extracted documents must
carry parent_document_id pointing at the primary document the LLM read from."

Derivation: src/provenance/llm_extracted_parent.py::resolve_parent — matches on
an exact derived processed-transcript basename for call summaries, or the
canonical IR source path plus stored period for investor-update summaries. Zero
or ambiguous candidates remain unresolved; no fiscal-calendar fallback is
permitted.

This is a document-lineage repair only. It does not mutate legacy facts: a
period correction for historical KPI records requires a separately governed
fact-plane publication rather than a new legacy-fact write path.

Rows with zero or ambiguous candidate parents are left NULL and printed in the
report — no guessing. Apply mode updates only orphan rows and is idempotent;
re-running after new primary documents land can resolve previously blocked rows.

Usage:
    python execution/backfill_llm_extracted_parents.py                 # dry run, all tickers
    python execution/backfill_llm_extracted_parents.py --apply
    python execution/backfill_llm_extracted_parents.py --ticker NU --apply
    python execution/backfill_llm_extracted_parents.py --db C:/path/to/portfolio.db --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from provenance.llm_extracted_parent import resolve_parent  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _new_counter() -> Counter[str]:
    return Counter()


@dataclass(slots=True)
class BackfillResult:
    """Aggregate outcome of one backfill pass."""

    scanned: int = 0
    resolved: int = 0
    unresolved: int = 0
    confidence_counts: Counter[str] = field(default_factory=_new_counter)
    unresolved_rows: list[tuple[int, str, str, str]] = field(
        default_factory=list[tuple[int, str, str, str]]
    )


def backfill(
    db_path: Path,
    *,
    only_ticker: str | None = None,
    dry_run: bool = True,
    log: bool = True,
) -> BackfillResult:
    """Walk orphan llm_extracted documents rows and fill parent_document_id."""
    role = SQLiteConnectionRole.READ_ONLY if dry_run else SQLiteConnectionRole.WRITER
    conn = connect_sqlite(db_path, role=role, schema_preflight=not dry_run)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT id, ticker, doc_type, period_end, file_path FROM documents "
            "WHERE source_type = 'llm_extracted' AND parent_document_id IS NULL"
        )
        params: tuple[str, ...] = ()
        if only_ticker is not None:
            sql += " AND ticker = ?"
            params = (only_ticker.upper(),)
        sql += " ORDER BY id"

        result = BackfillResult()
        for row in conn.execute(sql, params).fetchall():
            result.scanned += 1
            match = resolve_parent(
                conn,
                ticker=row["ticker"],
                doc_type=row["doc_type"],
                file_path=row["file_path"] or "",
                period_end=row["period_end"],
            )
            period_label = str(row["period_end"] or "")[:10]
            if match is None:
                result.unresolved += 1
                result.unresolved_rows.append(
                    (row["id"], row["ticker"], row["doc_type"], period_label)
                )
                if log:
                    print(
                        f"  UNRESOLVED id={row['id']} {row['ticker']} {row['doc_type']} "
                        f"{period_label} - no unique exact-basename transcript parent"
                    )
                continue
            result.resolved += 1
            result.confidence_counts[match.confidence] += 1
            if log:
                print(
                    f"  id={row['id']} {row['ticker']} {row['doc_type']} {period_label} "
                    f"-> parent={match.parent_document_id} ({match.confidence})"
                )
            if not dry_run:
                conn.execute(
                    "UPDATE documents SET parent_document_id = ? "
                    "WHERE id = ? AND parent_document_id IS NULL",
                    (match.parent_document_id, row["id"]),
                )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    if log:
        verb = "would resolve" if dry_run else "resolved"
        print(f"\nscanned {result.scanned} orphan llm_extracted documents row(s)")
        print(f"  {verb}: {result.resolved}")
        if result.confidence_counts:
            by_conf = ", ".join(f"{k}={v}" for k, v in sorted(result.confidence_counts.items()))
            print(f"    by confidence: {by_conf}")
        print(f"  unresolved (left NULL, no candidate parent found): {result.unresolved}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite DB path (default: <repo-root>/data/portfolio.db)",
    )
    parser.add_argument("--ticker", default=None, help="limit to one ticker")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the resolved parent_document_id values (default: dry run, report only)",
    )
    args = parser.parse_args()

    db_path = (args.db or (PROJECT_ROOT / "data" / "portfolio.db")).resolve()
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    mode = "applying" if args.apply else "dry-run"
    print(f"{mode} llm_extracted parent_document_id backfill on {db_path}")
    backfill(db_path, only_ticker=args.ticker, dry_run=not args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
