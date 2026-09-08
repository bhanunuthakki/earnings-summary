"""Rehearse KPI definition-revision readiness on an explicit reader snapshot."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

try:
    from execution._lib import command_parser, log_event
except ModuleNotFoundError:  # managed script launch adds execution/ as the import root
    from _lib import command_parser, log_event

from compute.kpi_revision_shadow_census import (
    audit_kpi_revision_shadow_census,
    verify_snapshot_evidence,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--effective-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--known-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--evaluated-at", type=datetime.fromisoformat, required=True)
    args = parser.parse_args(argv)
    database_path: Path = args.db_path.expanduser().resolve()
    manifest_path: Path = args.snapshot_manifest.expanduser().resolve()
    evidence = verify_snapshot_evidence(
        database_path=database_path,
        manifest_path=manifest_path,
    )
    log_event(
        "kpi_revision_shadow_census_started",
        database_path=database_path,
        snapshot_evidence_status=evidence.status,
    )
    conn = connect_sqlite(database_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        conn.execute("PRAGMA query_only=ON")
        result = audit_kpi_revision_shadow_census(
            conn,
            effective_at=args.effective_at,
            known_at=args.known_at,
            evaluated_at=args.evaluated_at,
            snapshot_evidence=evidence,
        )
    finally:
        conn.close()
    final_evidence = verify_snapshot_evidence(
        database_path=database_path,
        manifest_path=manifest_path,
    )
    if final_evidence != evidence:
        raise RuntimeError("snapshot identity changed during KPI revision shadow census")
    print(result.model_dump_json())
    log_event(
        "kpi_revision_shadow_census_finished",
        deterministic_readiness=result.deterministic_readiness.value,
        series_count=len(result.series),
        activation_state=result.activation_state,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
