"""Recompute every verifier and atomically seal investor-grade population cutover."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.population_cutover import (  # noqa: E402
    PopulationCutoverRequest,
    evaluate_population_cutover,
)
from runtime.job_runtime import JobLock  # noqa: E402
from search.embedding_promotion import LocalVectorRuntimeConfig  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--cutoff-at", type=_datetime, required=True)
    parser.add_argument("--observed-through-at", type=_datetime, required=True)
    parser.add_argument("--index-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--audit-sample-limit", type=int, default=20)
    parser.add_argument("--audit-fetch-size", type=int, default=250)
    parser.add_argument("--parity-page-size", type=int, default=1_000)
    parser.add_argument("--parity-max-pages", type=int, default=10_000)
    parser.add_argument("--parity-max-rows", type=int, default=1_000_000)
    return parser


def _run(args: argparse.Namespace):
    if (args.index_root is None) != (args.runtime_root is None):
        raise ValueError("--index-root and --runtime-root must be supplied together")
    runtime = (
        None
        if args.index_root is None
        else LocalVectorRuntimeConfig(
            index_root=args.index_root,
            runtime_root=args.runtime_root,
        )
    )
    request = PopulationCutoverRequest(
        knowledge_cutoff=args.cutoff_at,
        observed_through=args.observed_through_at,
        apply=bool(args.apply),
        audit_sample_limit=args.audit_sample_limit,
        audit_fetch_size=args.audit_fetch_size,
        parity_page_size=args.parity_page_size,
        parity_max_pages=args.parity_max_pages,
        parity_max_rows=args.parity_max_rows,
        retrieval_runtime=runtime,
    )
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=bool(args.apply))
    try:
        conn.execute("PRAGMA query_only = OFF" if args.apply else "PRAGMA query_only = ON")
        return evaluate_population_cutover(conn, request)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _event(
        "population_cutover_evaluation_started",
        mode="apply" if args.apply else "dry_run",
    )
    try:
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "population-cutover-receipts",
                [f"sqlite:{args.db.resolve()}", "population-cutover"],
            ):
                result = _run(args)
        else:
            result = _run(args)
    except Exception as exc:
        _event(
            "population_cutover_evaluation_failed",
            error_type=type(exc).__name__,
        )
        return 1
    _event(
        "population_cutover_evaluation_completed",
        blocker_count=len(result.blockers),
        cutover_ready=result.cutover_ready,
        outcome=result.outcome,
        population_run_id=None if result.run is None else result.run.population_run_id,
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 2 if result.outcome == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
