"""Populate the hardened source-fact plane from governed legacy reports."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.population_source_facts import (  # noqa: E402
    SourceFactPopulationBatchError,
    SourceFactPopulationRequest,
    populate_source_fact_plane,
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
    parser.add_argument("--after-extraction-run-id")
    parser.add_argument("--max-runs", type=int)
    parser.add_argument("--input-commitment-sha256")
    parser.add_argument("--planned-output-commitment-sha256")
    parser.add_argument("--apply", action="store_true")
    return parser


def _run(args: argparse.Namespace):
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=bool(args.apply))
    try:
        return populate_source_fact_plane(
            conn,
            SourceFactPopulationRequest(
                apply=bool(args.apply),
                data_cutoff_at=args.cutoff_at,
                operation_recorded_at=args.recorded_at,
                after_extraction_run_id=args.after_extraction_run_id,
                max_runs=args.max_runs,
                input_commitment_sha256=args.input_commitment_sha256,
                planned_output_commitment_sha256=(args.planned_output_commitment_sha256),
            ),
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _event(
        "source_fact_population_started",
        mode="apply" if args.apply else "dry_run",
        max_runs=args.max_runs,
    )
    try:
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "source-fact-population",
                [f"sqlite:{args.db.resolve()}"],
            ):
                result = _run(args)
        else:
            result = _run(args)
    except SourceFactPopulationBatchError as exc:
        _event(
            "source_fact_population_partially_failed",
            error_type=type(exc.__cause__).__name__,
            detail=redact(exc.__cause__ or exc),
            checkpoint=exc.checkpoint_payload(),
        )
        return 1
    except Exception as exc:
        _event(
            "source_fact_population_failed",
            error_type=type(exc).__name__,
            detail=redact(exc),
        )
        return 1
    _event(
        "source_fact_population_completed",
        eligible=result.eligible_count,
        excluded=result.excluded_count,
        processed_runs=result.processed_run_count,
        processed_observations=result.processed_observation_count,
        created_records=result.created_record_count,
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
