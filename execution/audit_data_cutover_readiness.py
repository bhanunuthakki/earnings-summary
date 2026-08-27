"""Run the exact, dual-clock 13-gate data-cutover readiness audit."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.integrity_audit import (  # noqa: E402
    CutoverAuditOptions,
    audit_cutover_readiness,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr)


def _configure_read_only(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA temp_store = MEMORY")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--knowledge-cutoff", type=datetime.fromisoformat, required=True)
    parser.add_argument("--observed-through", type=datetime.fromisoformat, required=True)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--fetch-size", type=int, default=250)
    args = parser.parse_args(argv)
    options = CutoverAuditOptions(
        knowledge_cutoff=args.knowledge_cutoff,
        observed_through=args.observed_through,
        sample_limit=args.sample_limit,
        fetch_size=args.fetch_size,
    )
    _event(
        "data_cutover_readiness_audit_started",
        db_path=str(args.db_path),
        knowledge_cutoff=options.knowledge_cutoff.isoformat(),
        observed_through=options.observed_through.isoformat(),
    )
    conn = connect_sqlite(args.db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        _configure_read_only(conn)
        summary = audit_cutover_readiness(conn, options)
    finally:
        conn.close()
    print(summary.model_dump_json())
    _event(
        "data_cutover_readiness_audit_finished",
        eligible_count=sum(item.eligible_count for item in summary.coverage),
        failed_count=sum(item.failed_count for item in summary.coverage),
        finding_count=len(summary.findings),
        has_blockers=summary.has_blockers,
    )
    return 2 if summary.has_blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
