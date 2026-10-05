"""Retain a reviewed generic equity cash-flow model through governed persistence."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from db_paths import require_db_path
from dcf.cashflow_refresh import PreparedCashflowDcfRequest, prepare_cashflow_dcf
from dcf.input_evidence import SourceReadContext
from provenance.immutable_artifact import population_database_lock_resources
from runtime.job_runtime import JobLock
from runtime.secrets import load_project_env
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--state-root", type=Path, help="Explicit model source-byte read authority."
    )
    args = parser.parse_args(argv)
    load_project_env(args.repo_root)
    configured = args.db or os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    if not configured:
        raise RuntimeError("An explicit configured portfolio database is required")
    source_root = args.state_root or os.environ.get("DCF_SOURCE_STATE_ROOT")
    source_context = (
        SourceReadContext.for_sec_state_root(Path(source_root)) if source_root else None
    )
    database = require_db_path(configured)
    request = PreparedCashflowDcfRequest.model_validate_json(args.request.read_bytes())
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    with JobLock(
        args.repo_root,
        "prepare-cashflow-dcf",
        list(population_database_lock_resources(database, database)),
    ):
        conn = connect_sqlite(database, role=role)
        try:
            result = prepare_cashflow_dcf(
                conn,
                request,
                request_path=args.request,
                expected_request_sha256=args.request_sha256,
                repo_root=args.repo_root,
                artifact_path=args.artifact,
                apply=args.apply,
                source_context=source_context,
            )
            print(result.model_dump_json(indent=2))
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
