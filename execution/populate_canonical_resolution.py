"""Populate issuer-scoped canonical resolutions and projections."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.population_canonical_resolution import (  # noqa: E402
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc


def _run(args: argparse.Namespace) -> int:
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=args.apply)
    try:
        result = populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=args.cutoff_at,
                operation_recorded_at=args.operation_recorded_at,
                apply=args.apply,
                phase=args.phase,
                after_canonical_metric_cell_id=args.after_cell_id,
                max_cells=args.max_cells,
                input_commitment_sha256=args.input_commitment_sha256,
                plan_commitment_sha256=args.plan_commitment_sha256,
            ),
        )
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
        "--phase",
        choices=("resolutions", "snapshots", "projections", "all"),
        default="all",
    )
    parser.add_argument("--after-cell-id")
    parser.add_argument("--max-cells", type=int)
    parser.add_argument("--input-commitment-sha256")
    parser.add_argument("--plan-commitment-sha256")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        return _run(args)
    try:
        with JobLock(
            PROJECT_ROOT,
            "populate-canonical-resolution",
            ["portfolio-db", f"sqlite:{args.db.resolve()}"],
        ):
            return _run(args)
    except JobAlreadyRunningError as exc:
        sys.stderr.write(
            json.dumps(
                {"event": "canonical_resolution_population_deferred", "reason": str(exc)},
                sort_keys=True,
            )
            + "\n"
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
