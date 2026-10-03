"""Plan offline, then apply/resume one exact selected SEC accession for reading."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import requests

try:
    from _lib import PROJECT_ROOT, command_parser, log_event
except ImportError:
    from execution._lib import PROJECT_ROOT, command_parser, log_event

from db_paths import require_db_path
from operations.paths import operations_runtime_directory
from pipeline.sec_accession_refresh import (
    AccessionRefreshPlan,
    AccessionRefreshRequest,
    RefreshBoundaryError,
    apply_accession_refresh,
    inspect_accession_refresh,
    plan_accession_refresh,
    verify_plan,
)
from provenance.immutable_artifact import (
    assert_artifact_unchanged,
    publish_text_no_clobber,
    read_stable_artifact,
    require_canonical_text_artifact,
    require_no_reparse_points,
)
from provenance.sec_native_capture import SessionLike
from run_lock import RunLockHeldError, hold_run_lock
from runtime.job_runtime import (
    JobAlreadyRunningError,
    JobLock,
    inherited_lock_is_valid,
    portfolio_db_path,
)
from sec_identity import sec_user_agent
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True, help="Explicit product-state root")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--ticker")
    parser.add_argument("--issuer-id")
    parser.add_argument("--cik")
    parser.add_argument("--inventory-key")
    parser.add_argument("--accession-number")
    parser.add_argument("--capture-batch-size", type=int)
    parser.add_argument("--extraction-batch-size", type=int)
    parser.add_argument("--max-document-bytes", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-sha256", help="Exact plan commitment printed by offline planning")
    args = parser.parse_args(argv)
    try:
        database = require_db_path(args.db)
        root = args.repo_root.absolute()
        require_no_reparse_points(root)
        root = root.resolve(strict=True)
        # Validate the path segment before constructing any operation artifact path.
        if not isinstance(args.request_id, str) or not args.request_id or len(args.request_id) > 64:
            raise RefreshBoundaryError("invalid_request_id")
        if any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in args.request_id
        ):
            raise RefreshBoundaryError("invalid_request_id")
        operation_root = (
            operations_runtime_directory(root) / "sec-accession-refresh" / args.request_id
        )
        plan_path = operation_root / "plan.json"
        require_no_reparse_points(plan_path)
        with ExitStack() as stack:
            if args.apply:
                inherited = portfolio_db_path(
                    PROJECT_ROOT
                ).resolve() == database and inherited_lock_is_valid(PROJECT_ROOT, "portfolio-db")
                if not inherited:
                    stack.enter_context(
                        hold_run_lock(database, owner="sec-accession-refresh", timeout_s=0)
                    )
            stack.enter_context(
                JobLock(
                    PROJECT_ROOT,
                    "sec-accession-refresh",
                    [
                        f"artifact:{operation_root}",
                        *(
                            [
                                "sec-edgar-network",
                                f"sqlite:{database}",
                                f"evidence-blobs:{root / 'data/evidence/blobs'}",
                            ]
                            if args.apply
                            else []
                        ),
                    ],
                    wait_s=0,
                )
            )
            conn = connect_sqlite(
                database,
                role=(
                    SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
                ),
                schema_preflight=args.apply,
            )
            try:
                if not args.apply:
                    if args.plan_sha256:
                        raise RefreshBoundaryError("plan_commitment_is_apply_only")
                    request = AccessionRefreshRequest(
                        request_id=args.request_id,
                        repo_root=root,
                        ticker=args.ticker,
                        issuer_id=args.issuer_id,
                        cik=args.cik,
                        inventory_key=args.inventory_key,
                        accession_number=args.accession_number,
                        capture_batch_size=args.capture_batch_size
                        if args.capture_batch_size is not None
                        else 25,
                        extraction_batch_size=args.extraction_batch_size
                        if args.extraction_batch_size is not None
                        else 25,
                        max_document_bytes=args.max_document_bytes
                        if args.max_document_bytes is not None
                        else 100_000_000,
                    )
                    plan = plan_accession_refresh(conn, request)
                    result = inspect_accession_refresh(conn, plan)
                    publish_text_no_clobber(plan_path, plan.model_dump_json())
                    print(result.model_dump_json())
                    return 0
                if any(
                    (
                        args.ticker,
                        args.issuer_id,
                        args.cik,
                        args.inventory_key,
                        args.accession_number,
                    )
                ):
                    raise RefreshBoundaryError("apply_uses_only_frozen_selection")
                if any(
                    value is not None
                    for value in (
                        args.capture_batch_size,
                        args.extraction_batch_size,
                        args.max_document_bytes,
                    )
                ):
                    raise RefreshBoundaryError("apply_uses_only_frozen_budgets")
                snapshot, raw = read_stable_artifact(plan_path)
                plan = AccessionRefreshPlan.model_validate_json(raw)
                require_canonical_text_artifact(snapshot, plan.model_dump_json())
                if (
                    plan.request.repo_root != root
                    or plan.request.request_id != args.request_id
                    or plan.database_path != str(database)
                    or plan.commitment != args.plan_sha256
                ):
                    raise RefreshBoundaryError("plan_commitment_or_target_mismatch")
                verify_plan(conn, plan)
                contact = sec_user_agent()
                attempt_id = uuid4().hex
                attempt = operation_root / "attempts" / attempt_id
                common = {
                    "attempt_id": attempt_id,
                    "request_id": args.request_id,
                    "plan_sha256": plan.commitment,
                }
                publish_text_no_clobber(
                    attempt.with_suffix(".started.json"),
                    json.dumps(
                        {
                            **common,
                            "state": "running",
                            "recorded_at": datetime.now(UTC).isoformat(),
                        },
                        sort_keys=True,
                    ),
                )
                try:
                    with requests.Session() as session:
                        result = apply_accession_refresh(
                            conn, plan, session=cast(SessionLike, session), user_agent=contact
                        )
                    assert_artifact_unchanged(snapshot)
                    publish_text_no_clobber(
                        attempt.with_suffix(".result.json"),
                        json.dumps(
                            {
                                **common,
                                "recorded_at": datetime.now(UTC).isoformat(),
                                "result": result.model_dump(mode="json"),
                            },
                            sort_keys=True,
                        ),
                    )
                except BaseException as exc:
                    publish_text_no_clobber(
                        attempt.with_suffix(".unconfirmed.json"),
                        json.dumps(
                            {
                                **common,
                                "state": "completion_unconfirmed",
                                "reason_code": type(exc).__name__,
                                "recorded_at": datetime.now(UTC).isoformat(),
                            },
                            sort_keys=True,
                        ),
                    )
                    raise
                print(result.model_dump_json())
                return 0 if result.state == "succeeded" else 2
            finally:
                conn.close()
    except (JobAlreadyRunningError, RunLockHeldError):
        log_event("sec_accession_refresh_blocked", reason_code="locked")
        return 75
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        log_event("sec_accession_refresh_blocked", reason_code=type(exc).__name__)
        return 2


if __name__ == "__main__":
    sys.exit(main())
