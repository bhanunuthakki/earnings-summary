"""Validate, capture, extract, and resume one sealed SEC accession."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import requests

try:
    from _lib import PROJECT_ROOT, command_parser
except ImportError:
    from execution._lib import PROJECT_ROOT, command_parser

from pydantic import TypeAdapter

from filings.inline_xbrl_processor import ProcessorPackageMember
from filings.processor_installation import (
    InlineXbrlProcessorError,
    resolve_processor_installation,
)
from provenance.financial_statement_admission import FinancialStatementContextReview
from provenance.immutable_artifact import ImmutableArtifactConflictError
from provenance.sec_accession_refresh import (
    SecAccessionRefreshError,
    SecAccessionRefreshRequest,
    refresh_sec_accession,
)
from provenance.sec_native_capture import SessionLike
from runtime.job_runtime import JobAlreadyRunningError
from sec_identity import sec_user_agent

_MEMBERS = TypeAdapter(tuple[ProcessorPackageMember, ...])
_REVIEWS = TypeAdapter(tuple[FinancialStatementContextReview, ...])


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--inventory-key", required=True)
    parser.add_argument("--accession", required=True)
    parser.add_argument("--cik", required=True)
    parser.add_argument("--form-type", default=None)
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--checkpoint-root",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "sec_accession_refresh",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument("--installation", type=Path, default=None)
    parser.add_argument("--bundle-manifest", type=Path, default=None)
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument("--sandbox-launcher", type=Path, default=None)
    parser.add_argument("--sandbox-state-root", type=Path, default=None)
    parser.add_argument("--bundle-python", type=Path, default=None)
    parser.add_argument("--offline-artifact-manifest", type=Path, default=None)
    parser.add_argument("--statement-context-reviews", type=Path, default=None)
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--cancel", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    try:
        offline_artifacts = (
            ()
            if args.offline_artifact_manifest is None
            else _MEMBERS.validate_json(args.offline_artifact_manifest.read_bytes())
        )
        reviews = (
            ()
            if args.statement_context_reviews is None
            else _REVIEWS.validate_json(args.statement_context_reviews.read_bytes())
        )
        approved_bundle = None
        runtime_root: Path | None = None
        bundle_python: Path | None = None
        sandbox_launcher: Path | None = None
        sandbox_state_root: Path | None = None
        if not bool(args.capture_only) and not bool(args.cancel):
            has_install_flags = any(
                x is not None
                for x in (
                    args.installation,
                    args.bundle_manifest,
                    args.runtime_root,
                    args.sandbox_launcher,
                )
            )
            if bool(args.apply) or bool(args.extract_only) or has_install_flags:
                resolved = resolve_processor_installation(
                    project_root=PROJECT_ROOT,
                    installation_path=args.installation,
                    bundle_manifest=args.bundle_manifest,
                    runtime_root=args.runtime_root,
                    sandbox_launcher=args.sandbox_launcher,
                    sandbox_state_root=args.sandbox_state_root,
                    bundle_python=args.bundle_python,
                )
                approved_bundle = resolved.approved_bundle
                runtime_root = resolved.installation.runtime_root
                bundle_python = resolved.bundle_python
                sandbox_launcher = resolved.installation.sandbox_launcher
                sandbox_state_root = resolved.installation.sandbox_state_root
        request = SecAccessionRefreshRequest(
            db_path=args.db,
            inventory_key=str(args.inventory_key),
            accession_number=str(args.accession),
            expected_cik=str(args.cik).strip().zfill(10),
            expected_form_type=args.form_type,
            expected_ticker=args.ticker,
            task_id=str(args.task_id),
            checkpoint_root=args.checkpoint_root,
            blob_root=args.blob_root,
            user_agent=sec_user_agent(),
            project_root=PROJECT_ROOT,
            apply=bool(args.apply),
            capture_only=bool(args.capture_only),
            extract_only=bool(args.extract_only),
            cancel_pending=bool(args.cancel),
            offline_artifacts=offline_artifacts,
            statement_context_reviews=reviews,
            runtime_root=runtime_root,
            bundle_python=bundle_python,
            sandbox_launcher=sandbox_launcher,
            sandbox_state_root=sandbox_state_root,
            recorded_at=datetime.now(UTC),
        )
    except (OSError, ValueError, InlineXbrlProcessorError, ImmutableArtifactConflictError) as exc:
        _event(
            "sec_accession_refresh_rejected",
            reason_code="request_invalid",
            error=type(exc).__name__,
        )
        return 3

    try:
        with requests.Session() as session:
            result = refresh_sec_accession(
                request,
                session=cast(SessionLike, session),
                approved_bundle=approved_bundle,
            )
    except JobAlreadyRunningError:
        _event("sec_accession_refresh_locked", accession=request.accession_number)
        return 75
    except SecAccessionRefreshError as exc:
        _event("sec_accession_refresh_refused", reason_code=exc.reason_code)
        return 2

    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.status in {"completed", "cancelled"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
