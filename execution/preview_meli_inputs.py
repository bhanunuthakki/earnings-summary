"""Preview exact reported MELI input bindings without acquisition or model writes."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

try:
    from _lib import PROJECT_ROOT, command_parser
except ImportError:
    from execution._lib import PROJECT_ROOT, command_parser

from dcf.meli_input_preview import preview_meli_inputs
from provenance.immutable_artifact import publish_text_no_clobber
from provenance.population_cli_harness import validate_protected_receipt_path
from runtime.job_runtime import JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--period-end", type=date.fromisoformat, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = validate_protected_receipt_path(
        args.output,
        database=args.db,
        protected_receipts=(),
        conflict_message="input preview must not replace its database",
    )
    with JobLock(PROJECT_ROOT, "preview-meli-inputs", [f"artifact:{output}"]):
        conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
        try:
            conn.execute("BEGIN")
            preview = preview_meli_inputs(
                conn,
                research_snapshot_id=args.snapshot_id,
                financial_period_end=args.period_end,
                as_of=args.as_of,
            )
            publish_text_no_clobber(output, preview.model_dump_json(indent=2))
        finally:
            conn.close()
    sys.stdout.write(
        json.dumps(
            {
                "state": preview.state,
                "model_ready": preview.model_ready,
                "required_inputs": len(preview.slots),
                "matched_inputs": sum(slot.state == "matched" for slot in preview.slots),
                "output": str(output),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if preview.state == "reported_inputs_verified_not_model_ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
