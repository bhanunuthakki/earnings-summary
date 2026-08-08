"""Backfill exact evidence-document subject bindings from canonical registries."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.population_identity import (  # noqa: E402
    PopulationIdentityRequest,
    populate_recorded_subject_bindings,
)
from runtime.job_runtime import JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--cutoff-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--recorded-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def _run(args: argparse.Namespace):
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=bool(args.apply))
    try:
        request = PopulationIdentityRequest(
            apply=bool(args.apply),
            knowledge_cutoff=args.cutoff_at,
            operation_recorded_at=args.recorded_at,
        )
        if args.apply:
            with conn:
                return populate_recorded_subject_bindings(conn, request)
        return populate_recorded_subject_bindings(conn, request)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _event(
        "recorded_subject_binding_backfill_started",
        mode="apply" if args.apply else "dry_run",
    )
    try:
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "recorded-subject-binding-backfill",
                [f"sqlite:{args.db.resolve()}"],
            ):
                result = _run(args)
        else:
            result = _run(args)
    except Exception as exc:
        _event(
            "recorded_subject_binding_backfill_failed",
            error_type=type(exc).__name__,
        )
        return 1
    _event(
        "recorded_subject_binding_backfill_completed",
        selected=result.selected_count,
        unresolved=result.unresolved_count,
        conflicts=result.conflict_count,
        created=result.created_count,
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 2 if result.unresolved_count or result.conflict_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
