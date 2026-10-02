"""Select one analysis evidence set without fetching reports or writing financial data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from _lib import PROJECT_ROOT, command_parser
except ImportError:
    from execution._lib import PROJECT_ROOT, command_parser

from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope
from provenance.immutable_artifact import (
    assert_artifact_unchanged,
    publish_text_no_clobber,
    read_stable_artifact,
)
from provenance.population_cli_harness import validate_protected_receipt_path
from runtime.job_runtime import JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--scope-receipt", type=Path, required=True)
    args: argparse.Namespace = parser.parse_args(argv)
    output = validate_protected_receipt_path(
        args.scope_receipt,
        database=args.db,
        protected_receipts=(args.request,),
        conflict_message="scope receipt must not replace its request or database",
    )
    with JobLock(PROJECT_ROOT, "plan-analysis-evidence-scope", [f"artifact:{output}"]):
        snapshot, payload = read_stable_artifact(args.request)
        request = AnalysisScopeRequest.model_validate_json(payload)
        conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
        try:
            conn.execute("BEGIN")
            scope = build_analysis_scope(conn, request)
            assert_artifact_unchanged(snapshot)
            publish_text_no_clobber(output, scope.model_dump_json())
        finally:
            conn.close()
    sys.stdout.write(
        json.dumps(
            {
                "scope_id": scope.scope_id,
                "scope_receipt": str(output),
                "status": "selection_only_not_model_ready",
                "research_document_count": sum(
                    entry.role == "research_document" for entry in scope.entries
                ),
                "package_dependency_count": sum(
                    entry.role == "package_dependency" for entry in scope.entries
                ),
                "outside_scope_count": sum(
                    entry.role == "outside_scope" for entry in scope.entries
                ),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
