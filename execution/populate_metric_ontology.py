"""Populate the canonical metric ontology over the hardened source-fact plane."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.population_metric_ontology import (  # noqa: E402
    MetricOntologyPopulationRequest,
    MetricOntologyPopulationResult,
    populate_metric_ontology,
)
from runtime.job_runtime import JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--cutoff-at", type=_datetime, required=True)
    parser.add_argument("--recorded-at", type=_datetime, required=True)
    parser.add_argument(
        "--phase",
        choices=("registry", "assertions", "bindings", "snapshot", "all"),
        default="all",
    )
    parser.add_argument("--after-observation-id")
    parser.add_argument("--max-observations", type=int)
    parser.add_argument("--input-commitment-sha256")
    parser.add_argument("--plan-commitment-sha256")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _event(
        "metric_ontology_population_started",
        mode="apply" if args.apply else "dry_run",
        phase=args.phase,
    )
    try:

        def run() -> MetricOntologyPopulationResult:
            conn = connect_sqlite(
                args.db,
                role=(
                    SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
                ),
                schema_preflight=bool(args.apply),
            )
            try:
                return populate_metric_ontology(
                    conn,
                    MetricOntologyPopulationRequest(
                        apply=bool(args.apply),
                        knowledge_cutoff=args.cutoff_at,
                        operation_recorded_at=args.recorded_at,
                        phase=args.phase,
                        after_observation_id=args.after_observation_id,
                        max_observations=args.max_observations,
                        input_commitment_sha256=args.input_commitment_sha256,
                        plan_commitment_sha256=args.plan_commitment_sha256,
                    ),
                )
            finally:
                conn.close()

        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "metric-ontology-population",
                [f"sqlite:{args.db.resolve()}"],
            ):
                result = run()
        else:
            result = run()
    except Exception as exc:
        _event("metric_ontology_population_failed", error_type=type(exc).__name__)
        return 1
    _event(
        "metric_ontology_population_completed",
        phase=result.phase,
        outcome=result.outcome,
        reason_codes=result.reason_codes,
        snapshot_eligible=result.snapshot_eligible,
        processed=result.processed_observation_count,
        assertions=result.assertion_count,
        bindings=result.binding_count,
        snapshot_id=result.snapshot_id,
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 1 if result.outcome == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
