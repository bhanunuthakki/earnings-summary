"""Plan or apply explicitly reviewed MELI roles; no facts or models are written."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    from _lib import PROJECT_ROOT, command_parser
except ImportError:
    from execution._lib import PROJECT_ROOT, command_parser

from log_redact import redact
from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    assert_artifact_unchanged,
    publish_text_no_clobber,
    read_stable_artifact,
)
from provenance.meli_role_admission import (
    ReviewedRoleAdmission,
    RoleAdmissionRequest,
    apply_reviewed_meli_role_admission,
    plan_meli_role_admission,
)
from provenance.population_cli_harness import validate_protected_receipt_path
from runtime.job_runtime import JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        output = validate_protected_receipt_path(
            args.output,
            database=args.db,
            protected_receipts=(args.artifact,),
            conflict_message="role receipt aliases its database or reviewed artifact",
        )
        if output.exists():
            raise ImmutableArtifactConflictError("role admission requires a new receipt path")
        pinned, raw = read_stable_artifact(args.artifact)
        review = ReviewedRoleAdmission.model_validate_json(raw) if args.apply else None
        request = review.plan.request if review else RoleAdmissionRequest.model_validate_json(raw)
        with JobLock(
            PROJECT_ROOT,
            "meli-role-admission",
            [f"sqlite:{args.db.resolve()}", f"artifact:{output}"],
        ):
            conn = connect_sqlite(
                args.db,
                role=SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY,
                schema_preflight=args.apply,
            )
            conn.execute("BEGIN IMMEDIATE" if args.apply else "BEGIN")
            assert_artifact_unchanged(pinned)
            if review is not None:
                receipt = apply_reviewed_meli_role_admission(conn, review, as_of=datetime.now(UTC))
                assert_artifact_unchanged(pinned)
                conn.commit()
                publish_text_no_clobber(output, receipt.model_dump_json())
                state, status = "roles_applied_snapshot_required", 0
            else:
                plan = plan_meli_role_admission(conn, request)
                assert_artifact_unchanged(pinned)
                publish_text_no_clobber(output, plan.model_dump_json())
                state, status = plan.state, 0 if plan.state == "planned" else 2
            sys.stdout.write(json.dumps({"state": state, "model_ready": False}) + "\n")
            return status
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        # A post-commit artifact failure leaves replayable definition markers.
        sys.stdout.write(json.dumps({"state": "unavailable", "reason": redact(str(exc))}) + "\n")
        return 3
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
