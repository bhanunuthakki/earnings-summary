"""Match management commitments against realized kpi_facts.

Walks management_commitments where outcome IS NULL, looks up the
realized value from kpi_facts (matching ticker, kpi_name, period_target),
classifies HIT / MISS / BEAT / NO_DATA, and writes the verdict back to the
row.

The whole match is one bounded, deterministic database pass (no network, no
LLM), so it runs inside a single cross-process writer window: the daily brief
worker overlaps this subprocess with sibling chains on a bounded pool, and the
window takes the same per-database writer gate the pool driver and the other
chain steps honor. Reads and classification still happen inside the window —
the matcher's verdicts must land in one consistent batch — but nothing here
blocks on anything but the database, so the window stays short.

Usage:
    python execution/match_commitments.py
    python execution/match_commitments.py --ticker MELI
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

try:
    from _lib import PROJECT_ROOT
except ImportError:  # ``python -m execution.match_commitments``
    from execution._lib import PROJECT_ROOT

from compute.say_do import match_pending
from db_paths import configured_db_path
from pipeline.db_writer_gate import hold_writer_gate
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


@contextmanager
def _writer_window(db_path: Path) -> Generator[sqlite3.Connection]:
    """One gated writer window: the gate brackets the match-and-persist pass."""
    with hold_writer_gate(db_path, owner="match_commitments"):
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            yield conn
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Restrict to one ticker")
    parser.add_argument(
        "--db",
        default=str(configured_db_path(PROJECT_ROOT)),
        help=(
            "Portfolio database. The daily brief worker passes the resolved "
            "database explicitly; the default is the configured authority "
            "(EARNINGS_SUMMARY_DB_PATH, else the repo-root default for the "
            "production host)."
        ),
    )
    args = parser.parse_args()
    db_path = Path(args.db)

    with _writer_window(db_path) as conn:
        results = match_pending(conn, ticker=args.ticker)
    rows: list[dict[str, object]] = [
        {
            "commitment_id": r.commitment_id,
            "ticker": r.ticker,
            "kpi_name": r.kpi_name,
            "period_target": r.period_target.date().isoformat(),
            "target_value": str(r.target_value),
            "realized_value": str(r.realized_value) if r.realized_value is not None else None,
            "outcome": r.outcome.value,
        }
        for r in results
    ]
    outcome_counts: dict[str, int] = {}
    for r in rows:
        outcome = r["outcome"]
        if isinstance(outcome, str):
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

    print(
        json.dumps(
            {
                "matched": len(rows),
                "by_outcome": outcome_counts,
                "results": rows,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
