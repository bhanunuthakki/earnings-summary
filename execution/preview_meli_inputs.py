"""Preview exact reported MELI input bindings without acquisition or model writes."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path
from typing import Literal

try:
    from _lib import PROJECT_ROOT, command_parser
except ImportError:
    from execution._lib import PROJECT_ROOT, command_parser

from dcf.input_evidence import InputEvidenceError, SourceReadContext
from dcf.meli_input_preview import preview_meli_inputs
from provenance.fact_read_model import FactAdmissionError, ObservationUnavailableError
from provenance.immutable_artifact import publish_text_no_clobber
from provenance.population_cli_harness import validate_protected_receipt_path
from provenance.source_fact_publication import PublicationVerificationError
from runtime.job_runtime import JobLock
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_PRE_CANDIDATE_REASONS = frozenset(
    {
        "timezone_required",
        "unsupported_meli_fiscal_period",
        "research_snapshot_missing",
        "research_snapshot_unsealed",
        "research_snapshot_after_cutoff",
        "source_inventory_not_authoritative_complete",
        "source_inventory_stale_or_future",
        "source_inventory_superseded",
        "current_sec_inventory_missing",
        "financial_reporting_filing_date_missing",
        "financial_reporting_filing_date_invalid",
        "financial_reporting_filing_after_cutoff",
        "financial_reporting_package_authority_invalid",
        "financial_reporting_package_unbound",
        "financial_reporting_period_missing",
        "financial_reporting_period_after_cutoff",
        "financial_reporting_period_after_filing",
        "financial_reporting_classification_unavailable",
        "financial_reporting_package_authority_unavailable",
        "financial_anchor_not_latest_published_period",
    }
)
_SAFE_INPUT_REASONS = _PRE_CANDIDATE_REASONS | {
    "required_input_population_mismatch",
    "input_recipe_mismatch",
    "assumption_population_mismatch",
    "input_outside_canonical_snapshot",
    "input_snapshot_context_mismatch",
    "input_semantic_admission_failed",
    "input_coordinate_mismatch",
    "input_outside_source_universe",
    "annual_actual_required",
    "model_input_source_context_unavailable",
    "model_input_source_document_missing",
    "model_input_source_population_limit",
    "model_input_source_metadata_unavailable",
    "model_input_source_population_mismatch",
    "model_input_source_byte_limit",
    "model_input_source_location_unapproved",
    "model_input_source_bytes_unavailable",
    "model_input_source_digest_or_size_mismatch",
}


def _verification_reason(
    failure: InputEvidenceError
    | FactAdmissionError
    | ObservationUnavailableError
    | PublicationVerificationError,
) -> str:
    if isinstance(failure, InputEvidenceError):
        # Some typed errors append a document identity. Never release that
        # suffix, an unknown error message, or an underlying private path.
        reason = str(failure).partition(":")[0]
        return reason if reason in _SAFE_INPUT_REASONS else "input_verification_failed"
    if isinstance(failure, FactAdmissionError):
        return "fact_admission_failed"
    if isinstance(failure, ObservationUnavailableError):
        return "observation_unavailable_at_cutoff"
    return "source_publication_unverified"


def _unavailable(
    args: Namespace,
    reason: str,
    inspection: Literal["not_performed", "not_completed"],
) -> int:
    sys.stdout.write(
        json.dumps(
            {
                "state": "unavailable",
                "reason_code": reason,
                "model_ready": False,
                "research_snapshot_id": args.snapshot_id,
                "financial_period_end": args.period_end.isoformat(),
                "artifact_financial_as_of": args.as_of.isoformat(),
                "candidate_inspection": inspection,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--period-end", type=date.fromisoformat, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        source_context = SourceReadContext.for_product_state_root(args.state_root)
    except ValueError:
        return _unavailable(args, "source_authority_unavailable", "not_performed")
    output = validate_protected_receipt_path(
        args.output,
        database=args.db,
        protected_receipts=(),
        conflict_message="input preview must not replace its database",
    )
    reason = "input_verification_failed"
    with JobLock(PROJECT_ROOT, "preview-meli-inputs", [f"artifact:{output}"]):
        conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
        try:
            conn.execute("BEGIN")
            try:
                preview = preview_meli_inputs(
                    conn,
                    research_snapshot_id=args.snapshot_id,
                    financial_period_end=args.period_end,
                    as_of=args.as_of,
                    source_context=source_context,
                )
            except (
                InputEvidenceError,
                FactAdmissionError,
                ObservationUnavailableError,
                PublicationVerificationError,
            ) as failure:
                reason = _verification_reason(failure)
                preview = None
            else:
                # Publication conflicts and filesystem faults are command
                # failures, never missing financial evidence.
                publish_text_no_clobber(output, preview.model_dump_json(indent=2))
        finally:
            conn.close()
    if preview is None:
        return _unavailable(
            args,
            reason,
            "not_performed" if reason in _PRE_CANDIDATE_REASONS else "not_completed",
        )
    sys.stdout.write(
        json.dumps(
            {
                "state": preview.state,
                "model_ready": preview.model_ready,
                "required_inputs": len(preview.slots),
                "matched_inputs": sum(slot.state == "matched" for slot in preview.slots),
                "source_integrity": preview.source_integrity,
                "artifact_financial_as_of": preview.as_of.isoformat(),
                "raw_document_verifications": [
                    witness.model_dump(mode="json")
                    for witness in preview.raw_document_verifications
                ],
                "output": str(output),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if preview.state == "reported_inputs_verified_not_model_ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
