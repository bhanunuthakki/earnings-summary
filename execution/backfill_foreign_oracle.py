"""Compare selected retained foreign facts with the exact cached oracle, read-only.

Publication and canonical resolution remain separate governed producers. A
comparison never proves whole-population completion or current entitlement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from provenance.immutable_artifact import (
    assert_artifact_unchanged,
    publish_text_no_clobber,
    read_stable_artifact,
)
from sources.foreign_oracle_run import ForeignOracleManifest, compare_foreign_sources
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--input-manifest", type=Path)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--output-receipt",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "foreign_oracle_backfill_receipt.json",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    summary: dict[str, object] = {
        "status": "HOLD",
        "total_tickers_evaluated": 0,
        "total_exact_matches": 0,
        "receipts": [],
        "reason_codes": ["source_bound_input_manifest_and_database_required"],
    }
    if args.db is not None and args.input_manifest is not None:
        try:
            snapshot, payload = read_stable_artifact(args.input_manifest)
            manifest = ForeignOracleManifest.model_validate_json(payload)
            conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
            try:
                summary = compare_foreign_sources(conn, manifest, repo_root=args.repo_root)
                assert_artifact_unchanged(snapshot)
                summary["input_manifest_sha256"] = snapshot.file_sha256
                summary.pop("receipt_sha256", None)
                summary["receipt_sha256"] = hashlib.sha256(
                    json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str).encode()
                ).hexdigest()
            finally:
                conn.close()
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            summary = {
                "status": "HOLD",
                "total_tickers_evaluated": 0,
                "total_exact_matches": 0,
                "receipts": [],
                "reason_codes": ["source_bound_comparison_failed", type(exc).__name__],
            }
    rendered = json.dumps(summary, sort_keys=True, indent=2, default=str)
    publish_text_no_clobber(args.output_receipt, rendered)
    print(
        rendered
        if args.json
        else f"Foreign oracle comparison: {summary['status']}. Receipt: {args.output_receipt}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
