"""Publish exact captured foreign sources; dry-run by default, no acquisition.

The input manifest binds immutable document versions, expected native semantics,
and an optional qualified offline SEC processor. Canonical semantic resolution
remains a separate owner and is never inferred from source publication counts.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from filings.inline_xbrl_processor import load_approved_processor_bundle_manifest
from provenance.immutable_artifact import (
    assert_artifact_unchanged,
    population_database_lock_resources,
    publish_text_no_clobber,
    read_stable_artifact,
    validate_population_database_target,
)
from runtime.job_runtime import JobLock, portfolio_db_path
from sources.foreign_normalization_run import (
    ForeignNormalizationManifest,
    normalize_foreign_sources,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--input-manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--output-receipt",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "foreign_normalization_receipt.json",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    summary: dict[str, object] = {
        "status": "HOLD",
        "total_tickers_evaluated": 0,
        "receipts": [],
        "reason_codes": ["captured_input_manifest_and_database_required"],
    }
    code = 1
    try:
        if args.db is not None and args.input_manifest is not None:
            snapshot, payload = read_stable_artifact(args.input_manifest)
            manifest = ForeignNormalizationManifest.model_validate_json(payload)
            database = Path(os.path.abspath(args.db))
            locks = ["foreign-normalization:" + str(database)]
            if args.apply:
                locks.extend(
                    population_database_lock_resources(database, portfolio_db_path(PROJECT_ROOT))
                )
            for item in manifest.documents:
                if item.accession_number:
                    locks.append("filing-xbrl-accession:" + item.accession_number)
            if manifest.processor:
                approved = load_approved_processor_bundle_manifest(
                    manifest.processor.bundle_manifest
                )
                locks.append("filing-xbrl-bundle:" + approved.manifest.manifest_sha256)
                locks.append(
                    "filing-xbrl-package-cache:"
                    + str(
                        Path(
                            os.path.abspath(
                                manifest.processor.runtime_root.parent / "filing-xbrl-package-cache"
                            )
                        )
                    )
                )
            with JobLock(PROJECT_ROOT, "normalize-foreign-filings", locks):
                database = validate_population_database_target(
                    database, portfolio_db_path(PROJECT_ROOT)
                )
                assert_artifact_unchanged(snapshot)
                conn = connect_sqlite(
                    database,
                    role=SQLiteConnectionRole.WRITER
                    if args.apply
                    else SQLiteConnectionRole.READ_ONLY,
                    schema_preflight=bool(args.apply),
                )
                try:
                    receipt = normalize_foreign_sources(
                        conn,
                        manifest,
                        input_manifest_sha256=snapshot.file_sha256,
                        apply=bool(args.apply),
                    )
                    summary = receipt.model_dump(mode="json")
                    code = 0 if receipt.status == "DRY_RUN" else 1
                finally:
                    conn.close()
    except Exception as exc:
        summary["status"] = "HOLD"
        summary["reason_codes"] = ["normalization_failed", type(exc).__name__]
        code = 2
    rendered = json.dumps(summary, sort_keys=True, indent=2)
    publish_text_no_clobber(args.output_receipt, rendered)
    if args.json:
        print(rendered)
    else:
        print(f"Foreign source normalization: {summary['status']}. Receipt: {args.output_receipt}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
