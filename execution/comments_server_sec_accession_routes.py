"""Explicit plan/apply and read-only durable status for selected SEC text."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from flask import Flask, request
from pydantic import ValidationError

from dispatch_registry import Registry, RegistryConflict
from pipeline.sec_accession_refresh import RefreshBoundaryError
from pipeline.sec_accession_request import (
    MAX_ATTEMPTS,
    SecAccessionApplyInput,
    SecAccessionPlanInput,
    load_bound_request,
    operation_directory,
    prepare_bound_request,
    read_request_status,
    verify_bound_request,
)
from provenance.immutable_artifact import publish_text_no_clobber, require_no_reparse_points
from runtime.job_runtime import JobAlreadyRunningError, JobLock
from runtime.python_process import ManagedPythonUnavailableError, managed_python_argv


@dataclass(frozen=True)
class SecAccessionRouteContext:
    state_root: Path
    code_root: Path
    db_path: Path | None
    registry: Registry
    get_read_db: Callable[[], sqlite3.Connection]


def register_sec_accession_routes(app: Flask, context: SecAccessionRouteContext) -> None:
    """Use the server's local/CSRF guards and trusted roots; accept no client paths."""
    app.config["SEC_ACCESSION_REGISTRY"] = context.registry

    def registry() -> Registry:
        if not isinstance(app.config["SEC_ACCESSION_REGISTRY"], Registry):
            raise RefreshBoundaryError("registry_unavailable")
        return cast(Registry, app.config["SEC_ACCESSION_REGISTRY"])

    def require_database() -> Path:
        database = context.db_path
        if database is None:
            raise RefreshBoundaryError("explicit_database_required")
        require_no_reparse_points(database)
        if not database.is_absolute() or database.resolve(strict=True) != database:
            raise RefreshBoundaryError("invalid_database_context")
        return database

    def status(request_id: str) -> dict[str, object]:
        require_database()
        value = read_request_status(context.get_read_db(), context.state_root, request_id)
        job_id = value["job_id"]
        job = registry().get(job_id) if isinstance(job_id, str) else None
        if job is not None and job.startup_disposition["process_retained"]:
            value["launch_confirmed"] = True
        if (
            job is not None
            and job.is_running
            and value["state"] == "completion_unconfirmed"
            and value["launch_confirmed"]
            and job.startup_disposition["state"] == "started"
        ):
            value["state"] = "running"
        value["job_progress"] = job.snapshot() if job else None
        return value

    @app.route("/actions/sec-accession/plan", methods=["POST", "OPTIONS"])
    def sec_accession_plan():
        if request.method == "OPTIONS":
            return "", 204
        try:
            body = SecAccessionPlanInput.model_validate(request.get_json(silent=True))
        except ValidationError:
            return {"code": "invalid_request"}, 400
        try:
            require_database()
            operation = operation_directory(context.state_root, body.request_id)
            with JobLock(
                context.state_root,
                "sec-request-plan",
                [f"artifact:{operation}/http-control"],
                wait_s=0,
            ):
                prepare_bound_request(context.get_read_db(), context.state_root, body)
            value = status(body.request_id)
            value["status_url"] = f"/actions/sec-accession/{body.request_id}"
            return value, 200
        except (JobAlreadyRunningError, RegistryConflict):
            return {"code": "request_locked"}, 409
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return {"code": "request_plan_unavailable"}, 409

    @app.get("/actions/sec-accession/<request_id>")
    def sec_accession_status(request_id: str):
        try:
            return status(request_id), 200
        except FileNotFoundError:
            return {"code": "request_unknown", "state": "unknown"}, 404
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return {"code": "request_status_unavailable", "state": "unknown"}, 409

    @app.route("/actions/sec-accession/<request_id>/apply", methods=["POST", "OPTIONS"])
    def sec_accession_apply(request_id: str):
        if request.method == "OPTIONS":
            return "", 204
        try:
            body = SecAccessionApplyInput.model_validate(request.get_json(silent=True))
        except ValidationError:
            return {"code": "invalid_request"}, 400
        try:
            database = require_database()
            operation = operation_directory(context.state_root, request_id)
            with JobLock(
                context.state_root,
                "sec-request-dispatch",
                [f"artifact:{operation}/http-control"],
                wait_s=0,
            ):
                bound, plan, _snapshots = load_bound_request(context.state_root, request_id)
                if (
                    plan.commitment != body.plan_sha256
                    or bound.commitment != body.request_sha256
                    or bound.scope.scope_sha256 != body.scope_sha256
                    or plan.database_path != str(database)
                ):
                    raise RefreshBoundaryError("request_commitment_mismatch")
                # Check the CLI's operation lane, then release it before the child starts.
                # The CLI reacquires this same lock and rechecks every binding before transport.
                with JobLock(
                    context.code_root, "sec-request-preflight", [f"artifact:{operation}"], wait_s=0
                ):
                    verify_bound_request(context.get_read_db(), bound, plan)
                current = status(request_id)
                if current["state"] in ("running", "succeeded", "completion_unconfirmed"):
                    raise RefreshBoundaryError("request_not_resumable")
                if current["attempt_count"] == 0:
                    if body.action != "apply" or body.resume_from is not None:
                        raise RefreshBoundaryError("initial_apply_required")
                elif body.action != "resume" or body.resume_from != current["attempt_id"]:
                    raise RefreshBoundaryError("exact_resume_attempt_required")
                if (
                    not isinstance(current["attempt_count"], int)
                    or current["attempt_count"] >= MAX_ATTEMPTS
                ):
                    raise RefreshBoundaryError("attempt_population_over_budget")
                attempt_id = uuid4().hex
                argv = managed_python_argv(
                    context.code_root,
                    context.code_root / "execution/refresh_sec_accession.py",
                    "--db",
                    str(database),
                    "--repo-root",
                    str(context.state_root),
                    "--request-id",
                    request_id,
                    "--apply",
                    "--plan-sha256",
                    plan.commitment,
                    "--request-sha256",
                    bound.commitment,
                    "--attempt-id",
                    attempt_id,
                )
                job = registry().start(
                    ticker=plan.request.ticker,
                    kind=f"sec-accession-{request_id}",
                    argv=argv,
                    spawn=False,
                    cwd=str(context.code_root),
                    code_root=context.code_root,
                    write_sets=[
                        "portfolio-db",
                        "sec-edgar-network",
                        f"sqlite:{database}",
                        f"evidence-blobs:{context.state_root / 'data/evidence/blobs'}",
                    ],
                )
                common = {
                    "attempt_id": attempt_id,
                    "request_id": request_id,
                    "plan_sha256": plan.commitment,
                    "request_sha256": bound.commitment,
                }
                try:
                    publish_text_no_clobber(
                        operation / "attempts" / f"{attempt_id}.dispatch.json",
                        json.dumps(
                            {
                                **common,
                                "job_id": job.job_id,
                                "state": "dispatched",
                                "recorded_at": datetime.now(UTC).isoformat(),
                            },
                            sort_keys=True,
                        ),
                    )
                    job.start_subprocess()
                except BaseException as exc:
                    # start_subprocess can fail after creating its child. Keep the
                    # owner's distinction between no birth and unconfirmed closure.
                    job.record_startup_failure(exc)
                    startup = job.startup_disposition
                    publish_text_no_clobber(
                        operation / "attempts" / f"{attempt_id}.dispatch-failed.json",
                        json.dumps(
                            {
                                **common,
                                "state": "dispatch_failed"
                                if startup["state"] == "launch_failed"
                                else "completion_unconfirmed",
                                "reason_code": startup["reason_code"] or type(exc).__name__,
                                "recorded_at": datetime.now(UTC).isoformat(),
                            },
                            sort_keys=True,
                        ),
                    )
                    raise
                return {
                    "request_id": request_id,
                    "attempt_id": attempt_id,
                    "job_id": job.job_id,
                    "stream_url": f"/actions/stream/{job.job_id}",
                    "status_url": f"/actions/sec-accession/{request_id}",
                    "cancellation": "unavailable",
                    "financial_readiness": "missing",
                }, 202
        except (RegistryConflict, JobAlreadyRunningError):
            return {"code": "request_locked"}, 409
        except ManagedPythonUnavailableError:
            return {"code": "managed_python_unavailable"}, 503
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return {"code": "request_apply_refused"}, 409
