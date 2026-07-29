"""Backfill SEC CompanyFacts evidence bindings into an isolated SQLite target.

The default is an offline read-only dry run.  ``--apply`` fetches fresh SEC
CompanyFacts and writes only accession documents plus immutable evidence.
The canonical live ``data/portfolio.db`` is deliberately rejected.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.sec_companyfacts_binding_backfill import (  # noqa: E402
    CompanyFactsBindingBackfillRequest,
    backfill_sec_companyfacts_bindings,
    emit_structured_event,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Explicit isolated target SQLite path (the live portfolio DB is rejected)",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        required=True,
        help="Explicit immutable CompanyFacts blob destination",
    )
    parser.add_argument(
        "--checkpoint-root",
        type=Path,
        default=PROJECT_ROOT / ".tmp",
        help="Checkpoint parent directory",
    )
    parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="Optional ticker filter; repeat for multiple issuers",
    )
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument(
        "--task-id",
        default="sec-companyfacts-binding-backfill",
    )
    parser.add_argument("--request-timeout-seconds", type=int, default=30)
    parser.add_argument(
        "--minimum-request-interval-seconds",
        type=float,
        default=0.15,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Fetch fresh SEC bytes and persist this bounded batch",
    )
    args = parser.parse_args(argv)

    db_path = args.db.resolve()
    live_path = (PROJECT_ROOT / "data" / "portfolio.db").resolve()
    if db_path == live_path:
        emit_structured_event(
            "sec_companyfacts_binding_live_db_rejected",
            db_path=str(db_path),
        )
        return 2

    request = CompanyFactsBindingBackfillRequest(
        blob_root=args.blob_root,
        checkpoint_root=args.checkpoint_root,
        apply=args.apply,
        tickers=tuple(args.ticker),
        batch_size=args.batch_size,
        task_id=args.task_id,
        request_timeout_seconds=args.request_timeout_seconds,
        minimum_request_interval_seconds=args.minimum_request_interval_seconds,
    )
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    try:
        conn = connect_sqlite(
            db_path,
            role=role,
            schema_preflight=request.apply,
        )
        try:
            summary = backfill_sec_companyfacts_bindings(conn, request)
        finally:
            conn.close()
    except Exception as exc:
        emit_structured_event(
            "sec_companyfacts_binding_backfill_failed",
            error_type=type(exc).__name__,
            message=redact(exc),
        )
        return 1
    sys.stdout.write(summary.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
