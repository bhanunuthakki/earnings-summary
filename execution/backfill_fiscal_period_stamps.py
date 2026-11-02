"""execution/backfill_fiscal_period_stamps.py
---------------------------------------------
One-shot (re-runnable) correction of ``documents.period_end`` /
``kpi_facts.period_end`` for ``llm_extracted`` rows stamped on the WRONG
fiscal calendar before summary filename mappings covered AMAT/TOL (Oct-FYE
issuers). The current calendar owner is ``compute.fiscal_calendar``.

Background (directives/data_provenance.md, "Fiscal-period stamping drift"):
``execution/backfill_llm_extracted_parents.py`` (#765) left 10 llm_extracted
orphans with no resolvable ``parent_document_id``. 6 of those (AMAT ids
8296/8297, TOL ids 8382/8383) were NOT missing-source rows — they were
mis-stamped: the summary override table only covered RBRK/VEEV (Jan FYE),
so AMAT/TOL (Oct FYE) fell through to the plain calendar-quarter map. E.g.
AMAT_Q4_2025 (fiscal Q4 ending 2025-10-31, matching the registered transcript)
was stamped 2025-12-31 instead, so `resolve_parent`'s exact-date match never
found the transcript that was actually read.

This script:
  1. Re-derives the correct period_end from the row's own file_path (which
     encodes `<TICKER>_Q<N>_<YYYY>`) using the now-fixed
     `compute.fiscal_calendar.summary_period_end`.
  2. Where the stored period_end differs from the re-derived one AND the
     ticker is one of the historically-affected off-cycle-FYE tickers,
     reports the source-backed correction candidate.
  3. Never mutates historical observations. Apply has been retired; corrections
     use the reviewed append-only supersession path.

Scope is limited to ``compute.fiscal_calendar.FILENAME_CALENDARS``. The summary
filename label adapter supplies dates; no runtime registry expands this scan.

Usage:
    python execution/backfill_fiscal_period_stamps.py                 # dry run
    python execution/backfill_fiscal_period_stamps.py --ticker AMAT
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:  # direct script invocation
    from _lib import PROJECT_ROOT
except ImportError:  # pragma: no cover - package import fallback
    from execution._lib import PROJECT_ROOT

from compute.fiscal_calendar import FILENAME_CALENDARS, summary_period_end
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# Matches the `.tmp/<TICKER>_Q<N>_<YYYY>_*.txt` filename convention that
# `summary_period_end`'s (quarter, year) inputs come from — same regex shape as
# `compute/kpi_extract_summaries.py`'s `_SourceSpec` patterns, loosened to
# not care which of the 4 suffix variants follows.
_FILENAME_RE = re.compile(r"(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<year>20\d{2})_")


@dataclass(slots=True)
class CorrectionResult:
    scanned: int = 0
    corrected: int = 0
    kpi_facts_updated: int = 0
    unchanged: int = 0
    skipped_no_filename_match: list[int] = field(default_factory=list[int])


def _parse_filename(file_path: str) -> tuple[str, int, int] | None:
    m = _FILENAME_RE.search(Path(file_path).name)
    if m is None:
        return None
    return m.group("ticker"), int(m.group("q")), int(m.group("year"))


def backfill(
    db_path: Path,
    *,
    only_ticker: str | None = None,
    dry_run: bool = True,
    log: bool = True,
) -> CorrectionResult:
    """Re-stamp llm_extracted documents (+ dependent kpi_facts) whose period_end
    doesn't match what `summary_period_end` now computes from their own filename."""
    if not dry_run:
        raise ValueError("in-place fiscal-period repair is retired; use source-backed supersession")
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=False)
    conn.row_factory = sqlite3.Row
    result = CorrectionResult()
    try:
        tickers = [only_ticker.upper()] if only_ticker else sorted(FILENAME_CALENDARS)
        for ticker in tickers:
            if ticker not in FILENAME_CALENDARS:
                continue
            rows = conn.execute(
                "SELECT id, ticker, doc_type, period_end, file_path FROM documents "
                "WHERE source_type = 'llm_extracted' AND ticker = ? ORDER BY id",
                (ticker,),
            ).fetchall()
            for row in rows:
                result.scanned += 1
                parsed = _parse_filename(row["file_path"] or "")
                if parsed is None:
                    result.skipped_no_filename_match.append(row["id"])
                    continue
                _, quarter, year = parsed
                correct_period_end = summary_period_end(ticker, quarter, year)
                stored = str(row["period_end"] or "")[:10]
                correct_str = correct_period_end.date().isoformat()
                if stored == correct_str:
                    result.unchanged += 1
                    continue

                result.corrected += 1
                affected_facts = conn.execute(
                    "SELECT COUNT(*) FROM kpi_facts WHERE source_doc_id = ?", (row["id"],)
                ).fetchone()[0]
                if log:
                    print(
                        f"  id={row['id']} {ticker} {row['doc_type']} "
                        f"{stored} -> {correct_str} (file={Path(row['file_path']).name}, "
                        f"{affected_facts} kpi_facts row(s))"
                    )
                result.kpi_facts_updated += affected_facts
    finally:
        conn.close()

    if log:
        verb = "would correct" if dry_run else "corrected"
        print(f"\nscanned {result.scanned} llm_extracted documents row(s) for {tickers}")
        print(f"  {verb}: {result.corrected} document row(s)")
        print(f"  kpi_facts {'would be ' if dry_run else ''}updated: {result.kpi_facts_updated}")
        print(f"  already correct: {result.unchanged}")
        if result.skipped_no_filename_match:
            print(f"  skipped (unparseable file_path): {result.skipped_no_filename_match}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite DB path (default: <repo-root>/data/portfolio.db)",
    )
    parser.add_argument(
        "--ticker", default=None, help="limit to one ticker (must be Oct/Jan-FYE covered)"
    )
    args = parser.parse_args()

    db_path = (args.db or (PROJECT_ROOT / "data" / "portfolio.db")).resolve()
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    print(f"dry-run fiscal-period-stamp review on {db_path}")
    backfill(db_path, only_ticker=args.ticker, dry_run=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
