"""Import and reconcile one source-coverage inventory without hidden absence claims."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.source_coverage_reconcile import (  # noqa: E402
    load_source_coverage_import,
    reconcile_source_coverage,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True, help="Strict JSON source inventory")
    parser.add_argument("--apply", action="store_true", help="Append the immutable reconciliation")
    args = parser.parse_args(argv)
    source_import = load_source_coverage_import(args.input)
    request = source_import.model_copy(update={"apply": args.apply})
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    _event(
        "source_coverage_reconciliation_started",
        inventory_key=request.inventory_key,
        revision=request.revision,
        mode="apply" if request.apply else "dry_run",
    )
    conn = connect_sqlite(args.db, role=role, schema_preflight=request.apply)
    try:
        result = reconcile_source_coverage(conn, request)
    finally:
        conn.close()
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "source_coverage_reconciliation_finished",
        snapshot_id=result.snapshot_id,
        expected_document_count=result.expected_document_count,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
