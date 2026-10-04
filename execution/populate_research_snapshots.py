"""Populate exact issuer-scoped Research Snapshots for governed retrieval."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    from _lib import PROJECT_ROOT, command_parser
except ImportError:
    from execution._lib import PROJECT_ROOT, command_parser

from provenance.analysis_scope import AnalysisEvidenceScope
from provenance.immutable_artifact import (
    assert_artifact_unchanged,
    read_stable_artifact,
    require_canonical_text_artifact,
)
from provenance.population_research_snapshots import (
    ResearchSnapshotPopulationRequest,
    populate_research_snapshots,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def _datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc


def _run(args: argparse.Namespace) -> int:
    scope_path: Path | None = getattr(args, "analysis_scope", None)
    scope_snapshot = None
    analysis_scope = None
    if scope_path is not None:
        scope_snapshot, payload = read_stable_artifact(scope_path)
        analysis_scope = AnalysisEvidenceScope.model_validate_json(payload)
        require_canonical_text_artifact(scope_snapshot, analysis_scope.model_dump_json())

    def scope_unchanged() -> None:
        if scope_snapshot is not None:
            assert_artifact_unchanged(scope_snapshot)

    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=args.apply)
    try:
        request = ResearchSnapshotPopulationRequest(
            cutoff_at=args.cutoff_at,
            operation_recorded_at=args.operation_recorded_at,
            issuer_ids=tuple(args.issuer_id),
            apply=args.apply,
            projection_mode=args.projection_mode,
            input_commitment_sha256=args.input_commitment_sha256,
            plan_commitment_sha256=args.plan_commitment_sha256,
            analysis_scope=analysis_scope,
        )
        result = (
            populate_research_snapshots(conn, request)
            if analysis_scope is None
            else populate_research_snapshots(conn, request, before_publish=scope_unchanged)
        )
        if scope_snapshot is not None:
            assert_artifact_unchanged(scope_snapshot)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    return int(result.blocked_issuer_count > 0 or result.issuer_count == 0)


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--cutoff-at", type=_datetime, required=True)
    parser.add_argument(
        "--operation-recorded-at",
        "--recorded-at",
        dest="operation_recorded_at",
        type=_datetime,
        required=True,
    )
    parser.add_argument(
        "--projection-mode",
        choices=("semantic", "lexical_only"),
        default="semantic",
        help="Explicit retrieval mode; semantic requires vector and promotion evidence.",
    )
    parser.add_argument("--issuer-id", action="append", default=[])
    parser.add_argument("--input-commitment-sha256")
    parser.add_argument("--plan-commitment-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--analysis-scope", type=Path, help="Use one declared analysis evidence selection"
    )
    args = parser.parse_args(argv)
    if not args.apply:
        return _run(args)
    resources = ["portfolio-db", f"sqlite:{args.db.resolve()}"]
    if args.analysis_scope is not None:
        resources.append(f"artifact:{args.analysis_scope.absolute()}")
    try:
        with JobLock(
            PROJECT_ROOT,
            "populate-research-snapshots",
            resources,
        ):
            return _run(args)
    except JobAlreadyRunningError as exc:
        sys.stderr.write(
            json.dumps(
                {"event": "research_snapshot_population_deferred", "reason": str(exc)},
                sort_keys=True,
            )
            + "\n"
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
