"""Continue retained SEC matches through reviewed canonical statement admission."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from db_paths import require_db_path
from provenance.companyfacts_statement_continuation import (
    CompanyFactsStatementContinuationRequest,
    continue_companyfacts_statements,
)
from provenance.immutable_artifact import population_database_lock_resources
from runtime.job_runtime import JobLock
from runtime.secrets import load_project_env
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    load_project_env(args.repo_root)
    configured = args.db or os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    if not configured:
        raise RuntimeError("An explicit configured portfolio database is required")
    database = require_db_path(configured)
    request = CompanyFactsStatementContinuationRequest.model_validate_json(
        args.request.read_bytes()
    )
    request = request.model_copy(update={"apply": args.apply})
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    with JobLock(
        args.repo_root,
        "continue-companyfacts-statements",
        list(population_database_lock_resources(database, database)),
    ):
        conn = connect_sqlite(database, role=role)
        try:
            result = continue_companyfacts_statements(conn, request)
            print(result.model_dump_json(indent=2))
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
