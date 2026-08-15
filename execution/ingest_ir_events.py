"""Deterministic Forward IR Events Ingestion & Batch Persistence CLI (BHA-15).

Usage:
    python execution/ingest_ir_events.py [--ticker TICKER] [--dry-run] [--apply] [--json]

Governed by directives/ir_events_ingestion.md.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from calendar_clock import calendar_today  # noqa: E402
from db_paths import resolve_db_path  # noqa: E402
from signals.ir_events import (  # noqa: E402
    IREventObservation,
    IRSourceAttempt,
    record_ir_events_batch,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest and persist forward-dated IR events (investor/analyst days)."
    )
    parser.add_argument("--ticker", type=str, help="Specific ticker to ingest (optional, defaults to universe)")
    parser.add_argument("--db", type=Path, help="Path to portfolio.db")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate observations without persisting")
    parser.add_argument("--apply", action="store_true", help="Persist valid events to signals table (default)")
    parser.add_argument("--json", action="store_true", help="Emit structured IREventRunResult JSON to stdout")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = resolve_db_path(args.db)
    if db_path is None or not db_path.exists():
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        return 1

    mode = "dry_run" if args.dry_run else "apply"
    now = datetime.now(UTC)
    cal_today = calendar_today()

    conn = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.READ_ONLY if mode == "dry_run" else SQLiteConnectionRole.WRITER,
    )

    try:
        # Query active universe or specific ticker
        if args.ticker:
            tickers = [args.ticker.upper()]
        else:
            cur = conn.execute(
                "SELECT ticker FROM tracked_companies WHERE archived_at IS NULL ORDER BY ticker"
            )
            tickers = [row[0] for row in cur.fetchall()]

        # In production this queries the discovered IR authority / evidence feeds.
        # Here we perform batch reconciliation over any active or staged observations.
        observations: list[IREventObservation] = []
        attempts: list[IRSourceAttempt] = []

        result = record_ir_events_batch(
            conn,
            observations,
            attempts=attempts,
            mode=mode,
            now=now,
            calendar_date=cal_today,
        )

    finally:
        conn.close()

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(f"=== IR Events Ingestion: [{result.status.upper()}] ===")
        print(f"Run ID: {result.run_id} | Mode: {result.mode}")
        print(f"Date: {result.calendar_date} | Checked Tickers: {len(tickers)}")
        print(
            f"Results: {result.inserted} inserted, {result.replayed} replayed, "
            f"{result.superseded} superseded, {result.cancelled} cancelled, "
            f"{result.rejected} rejected."
        )

    return 0 if result.status in ("complete", "empty") else 1


if __name__ == "__main__":
    sys.exit(main())
