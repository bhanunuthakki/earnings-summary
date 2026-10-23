"""Inspect a named issuer cohort without collection, writes, or cutover claims."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

try:
    from execution._lib import command_parser
except ModuleNotFoundError:  # managed bootstrap exposes execution/ as the import root
    from _lib import command_parser

from provenance.cohort_readiness import audit_cohort_readiness
from provenance.population_completeness import PopulationTemporalScope
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument(
        "--issuer-id",
        action="append",
        required=True,
        help="Canonical issuer ID; repeat for each cohort member",
    )
    parser.add_argument("--knowledge-cutoff", type=datetime.fromisoformat, required=True)
    parser.add_argument("--observed-through", type=datetime.fromisoformat, required=True)
    args = parser.parse_args(argv)
    try:
        scope = PopulationTemporalScope(
            knowledge_cutoff=args.knowledge_cutoff, observed_through=args.observed_through
        )
        conn = connect_sqlite(args.db_path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("BEGIN")
            report = audit_cohort_readiness(conn, tuple(args.issuer_id), scope)
        finally:
            conn.close()
    except (ValueError, sqlite3.Error):
        print(
            json.dumps({"status": "blocked", "reason": "cohort_readiness_input_or_database_error"})
        )
        return 2
    print(report.model_dump_json())
    return 2 if report.has_blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
