"""Validate and publish one completely captured SEC Inline-XBRL accession.

Dry-run is the default.  SEC inventory and byte capture are explicit
prerequisites; this command admits only a sealed current inventory and an
offline, qualified processor bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from filings.inline_xbrl_processor import (
    InlineXbrlProcessorError,
    ProcessorPackageMember,
)
from filings.processor_installation import resolve_processor_installation
from provenance.financial_statement_admission import FinancialStatementContextReview
from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    population_database_lock_resources,
    validate_population_database_target,
)
from provenance.sec_filing_xbrl_ingest import (
    FilingXbrlIngestRequest,
    ingest_sec_filing_xbrl,
)
from runtime.job_runtime import (
    JobAlreadyRunningError,
    JobLock,
    portfolio_db_path,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_MEMBERS = TypeAdapter(tuple[ProcessorPackageMember, ...])
_REVIEWS = TypeAdapter(tuple[FinancialStatementContextReview, ...])


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--inventory-key")
    parser.add_argument("--accession")
    parser.add_argument("--cik")
    parser.add_argument("--installation", type=Path, help="One installed-bundle JSON descriptor")
    parser.add_argument("--preflight", action="store_true", help="Verify installed bytes only")
    parser.add_argument(
        "--statement-context-reviews", type=Path, help="Source-bound reviewed contexts"
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        help="Root whose complete file inventory is pinned by the processor runtime lock",
    )
    parser.add_argument("--bundle-python", type=Path)
    parser.add_argument(
        "--sandbox-launcher",
        type=Path,
        help="Hash-pinned OS sandbox launcher that enforces network denial",
    )
    parser.add_argument(
        "--bundle-manifest",
        type=Path,
        help="Canonical external bundle manifest approved by the committed review seal",
    )
    parser.add_argument(
        "--offline-artifact-manifest",
        type=Path,
        help="Optional JSON array of pinned standard-taxonomy/network package members",
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def _offline_artifacts(path: Path | None) -> tuple[ProcessorPackageMember, ...]:
    if path is None:
        return ()
    return _MEMBERS.validate_json(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        resolved = resolve_processor_installation(
            project_root=PROJECT_ROOT,
            installation_path=args.installation,
            bundle_manifest=args.bundle_manifest,
            runtime_root=args.runtime_root,
            sandbox_launcher=args.sandbox_launcher,
            bundle_python=args.bundle_python,
        )
    except (OSError, ValueError, InlineXbrlProcessorError, ImmutableArtifactConflictError) as exc:
        reason_code = (
            exc.reason_code
            if isinstance(exc, InlineXbrlProcessorError)
            else "installation_unavailable"
            if isinstance(exc, OSError)
            else "installation_evidence_invalid"
        )
        sys.stdout.write(
            json.dumps(
                {
                    "schema_version": "filing-xbrl-installation-preflight/v1",
                    "status": (
                        "unavailable"
                        if reason_code
                        in {"installation_unavailable", "installation_not_configured"}
                        else "rejected"
                    ),
                    "reason_code": reason_code,
                    "native_qualification": "not_run",
                    "decision_grade": False,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 3
    approved_bundle = resolved.approved_bundle
    if args.preflight:
        sys.stdout.write(
            json.dumps(
                {
                    "schema_version": "filing-xbrl-installation-preflight/v1",
                    "status": "ready_for_native_qualification",
                    "reason_code": "sealed_installation_verified",
                    "manifest_sha256": approved_bundle.manifest.manifest_sha256,
                    "runtime_artifact_sha256": approved_bundle.manifest.execution.runtime_artifact_sha256,
                    "native_qualification": "not_run",
                    "decision_grade": False,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    if any(value is None for value in (args.db, args.inventory_key, args.accession, args.cik)):
        _event("filing_xbrl_ingest_unavailable", reason_code="filing_arguments_incomplete")
        return 3
    database = Path(os.path.abspath(args.db))
    try:
        request = FilingXbrlIngestRequest(
            inventory_key=str(args.inventory_key),
            accession_number=str(args.accession),
            expected_cik=str(args.cik).strip().zfill(10),
            runtime_root=resolved.installation.runtime_root,
            bundle_python=resolved.bundle_python,
            sandbox_launcher=resolved.installation.sandbox_launcher,
            recorded_at=datetime.now(UTC),
            offline_artifacts=_offline_artifacts(args.offline_artifact_manifest),
            statement_context_reviews=(
                ()
                if args.statement_context_reviews is None
                else _REVIEWS.validate_json(args.statement_context_reviews.read_bytes())
            ),
            apply=bool(args.apply),
        )
    except (OSError, ValueError) as exc:
        _event(
            "filing_xbrl_ingest_unavailable",
            reason_code="filing_request_invalid",
            error_type=type(exc).__name__,
        )
        return 3
    write_sets = [
        f"filing-xbrl-accession:{request.accession_number}",
        f"filing-xbrl-bundle:{approved_bundle.manifest.manifest_sha256}",
        "filing-xbrl-package-cache:"
        + str(Path(os.path.abspath(request.runtime_root.parent / "filing-xbrl-package-cache"))),
    ]
    if request.apply:
        write_sets.extend(
            population_database_lock_resources(
                database,
                portfolio_db_path(PROJECT_ROOT),
            )
        )
    try:
        with JobLock(PROJECT_ROOT, "ingest-sec-filing-xbrl", write_sets):
            database = validate_population_database_target(
                database,
                portfolio_db_path(PROJECT_ROOT),
            )
            role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
            conn = connect_sqlite(
                database,
                role=role,
                schema_preflight=request.apply,
            )
            try:
                _event(
                    "filing_xbrl_ingest_started",
                    accession=request.accession_number,
                    mode="apply" if request.apply else "dry_run",
                )
                result = ingest_sec_filing_xbrl(
                    conn,
                    request,
                    approved_bundle=approved_bundle,
                )
            finally:
                conn.close()
    except JobAlreadyRunningError:
        _event("filing_xbrl_ingest_locked", accession=request.accession_number)
        return 75
    except Exception as exc:
        _event(
            "filing_xbrl_ingest_failed",
            accession=request.accession_number,
            error_type=type(exc).__name__,
        )
        return 2
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "filing_xbrl_ingest_completed",
        accession=request.accession_number,
        mode=result.mode,
        published=result.published_count,
        quarantined=result.quarantined_count,
        exact_replay=result.exact_replay,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
