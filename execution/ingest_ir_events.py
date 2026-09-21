"""Ingest verified captured IR event feeds; dry run unless explicitly enabled.

The upstream governed capture pipeline owns acquisition. No network request is
made here. Scheduled callers use the existing job runtime and portfolio-db lock.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

try:
    from _lib import PROJECT_ROOT, log_event
except ImportError:
    from execution._lib import PROJECT_ROOT, log_event

from provenance.immutable_artifact import publish_text_no_clobber
from run_lock import hold_run_lock
from runtime.job_runtime import inherited_lock_is_valid, portfolio_db_path
from signals.ir_event_discovery import discover_ir_events
from signals.ir_events import record_ir_events_batch
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ticker", help="One active tracked ticker; otherwise all active tracked companies"
    )
    parser.add_argument(
        "--db", type=Path, required=True, help="Explicit canonical or isolated test database"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / ".tmp" / "ir_events")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "apply" if args.apply else "dry_run"
    if args.apply and os.environ.get("IR_EVENTS_APPLY_ENABLED") != "1":
        print(
            json.dumps(
                {
                    "schema_version": "ir-events-discovery.v1",
                    "mode": mode,
                    "status": "disabled",
                    "reason_code": "apply_not_enabled",
                    "applied": False,
                }
            )
        )
        return 2
    if not args.db.is_file():
        print(
            json.dumps(
                {
                    "schema_version": "ir-events-discovery.v1",
                    "mode": mode,
                    "status": "unavailable",
                    "reason_code": "database_unavailable",
                    "applied": False,
                }
            )
        )
        return 2
    log_event("ir_events_started", mode=mode)
    try:
        inherited = args.apply and inherited_lock_is_valid(PROJECT_ROOT, "portfolio-db")
        if inherited and args.db.resolve() != portfolio_db_path(PROJECT_ROOT).resolve():
            raise ValueError("scheduler lock does not own requested database")
        with (
            hold_run_lock(args.db, owner="ir-events-ingest")
            if args.apply and not inherited
            else nullcontext()
        ):
            conn = connect_sqlite(
                args.db,
                role=SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY,
            )
            try:
                tickers = tuple(
                    str(row[0]).upper()
                    for row in conn.execute(
                        "SELECT DISTINCT ticker FROM tracked_companies WHERE archived_at IS NULL ORDER BY ticker"
                    )
                )
                if args.ticker:
                    requested = str(args.ticker).upper()
                    if requested not in tickers:
                        raise ValueError("requested ticker is not active tracked scope")
                    tickers = (requested,)
                if not tickers:
                    raise ValueError("active tracked scope is empty or unavailable")
                now = datetime.now(UTC)
                discovered = discover_ir_events(conn, tickers, now=now)
                result = record_ir_events_batch(
                    conn,
                    discovered.events,
                    attempts=discovered.attempts,
                    tickers=tickers,
                    mode=mode,
                    now=now,
                )
            finally:
                conn.close()
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        # Exceptions can contain upstream URL/query or file information; expose
        # only a typed failure category through this operator boundary.
        log_event("ir_events_failed", mode=mode, error_type=type(exc).__name__)
        print(
            json.dumps(
                {
                    "schema_version": "ir-events-discovery.v1",
                    "mode": mode,
                    "status": "failed",
                    "reason_code": type(exc).__name__,
                    "applied": False,
                }
            )
        )
        return 1
    log_event("ir_events_completed", mode=mode, status=result.status, attempt_id=result.attempt_id)
    payload = result.model_dump_json(indent=2)
    if len(payload.encode()) > 100_000 or len(payload.splitlines()) > 2000:
        destination = args.output_dir / result.run_id / f"{result.attempt_id}.json"
        publish_text_no_clobber(destination, payload)
        print(
            json.dumps(
                {
                    "schema_version": result.schema_version,
                    "run_id": result.run_id,
                    "attempt_id": result.attempt_id,
                    "mode": result.mode,
                    "status": result.status,
                    "freshness": result.freshness,
                    "receipt_path": str(destination),
                }
            )
        )
    else:
        print(
            payload
            if args.json
            else f"IR events {result.status}: {len(result.events)} discovered, {result.inserted} inserted, {result.replayed} replayed; {result.freshness} ({result.mode})"
        )
    return 0 if result.status in ("complete", "empty") else 2


if __name__ == "__main__":
    sys.exit(main())
