"""CLI entrypoint to verify the sealed FMP canary corpus and attribute source-regime cost.

Usage:
    python execution/attribute_source_cost.py
    python execution/attribute_source_cost.py --check-only
    python execution/attribute_source_cost.py --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from provenance.immutable_artifact import publish_text_no_clobber
from sources.canary_corpus import retained_coverage_inventory, seal_statement_corpus
from sources.telemetry import source_measurement_report
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def report_retained_sources(
    database: Path,
    *,
    document_ids: tuple[int, ...],
    cutoff_at: datetime,
    repo_root: Path,
    run_id: str | None = None,
) -> dict[str, object]:
    """Verify selected immutable bytes and read measurements; never acquire or mutate."""
    conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
    try:
        seal = seal_statement_corpus(
            conn,
            document_ids=document_ids,
            repo_root=repo_root,
            cutoff_at=cutoff_at,
        )
        measured_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_regime_measurements'"
        ).fetchone()
        measurements = (
            source_measurement_report(conn, run_id=run_id)
            if measured_table
            else {
                "status": "unavailable",
                "reason": "source_regime_measurements_schema_unavailable",
                "current_entitlement": "unverified",
                "output_readiness": "unverified",
            }
        )
        return {
            "status": "PARTIAL",
            "scope": "selected_retained_statement_bytes_and_measured_http_attempts",
            "canary_seal": seal.model_dump(mode="json"),
            "measurements": measurements,
            "coverage_inventory": retained_coverage_inventory(conn, cutoff_at=cutoff_at),
            "reason_codes": ["current_entitlement_unverified", "output_readiness_unverified"],
        }
    finally:
        conn.close()


MANIFEST_PATH = PROJECT_ROOT / "data" / "fmp_canary_manifest.json"
FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"
TMP_DIR = PROJECT_ROOT / ".tmp"


def verify_canary_corpus(manifest_path: Path, fmp_dir: Path) -> tuple[bool, list[str], int, int]:
    """Verify that all files declared in the manifest match byte-for-byte on disk."""
    if not manifest_path.exists():
        return False, [f"Manifest missing: {manifest_path}"], 0, 0

    try:
        manifest_data: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, [f"Manifest unparseable: {e}"], 0, 0

    files: list[dict[str, Any]] = manifest_data.get("files", [])
    if not files:
        return False, ["Manifest must contain a nonempty sealed file population"], 0, 0
    errors: list[str] = []
    verified_count = 0
    total_bytes = 0

    for item in files:
        fn = str(item["filename"])
        expected_sha = str(item["sha256"])
        fp = fmp_dir / fn
        if not fp.exists():
            errors.append(f"Missing file on disk: {fn}")
            continue

        raw = fp.read_bytes()
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != expected_sha:
            errors.append(
                f"SHA-256 mismatch for {fn}: expected {expected_sha[:12]}..., got {actual_sha[:12]}..."
            )
            continue

        verified_count += 1
        total_bytes += len(raw)

    return len(errors) == 0, errors, verified_count, total_bytes


def run_cost_attribution(verified_count: int, total_bytes: int) -> dict[str, Any]:
    """Record missing measured-cost evidence separately from verified cache bytes."""
    receipt_dict: dict[str, Any] = {
        "status": "HOLD",
        "verified_corpus_files": verified_count,
        "total_corpus_bytes": total_bytes,
        "summary": None,
        "reason_codes": ["measured_source_cost_evidence_unavailable"],
        "reason": (
            "File hashes prove cached bytes only. Measured source-regime latency, retries, "
            "provider/LLM cost, and operator-time evidence are not integrated."
        ),
    }
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    receipt_path = TMP_DIR / "source_regime_cost_receipt.json"
    receipt_path.write_text(json.dumps(receipt_dict, indent=2), encoding="utf-8")
    return receipt_dict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=MANIFEST_PATH, help="Path to canary manifest"
    )
    parser.add_argument(
        "--fmp-dir", type=Path, default=FMP_DIR, help="Path to FMP historical data directory"
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    parser.add_argument("--check-only", action="store_true", help="Only verify manifest hashes")
    parser.add_argument(
        "--db", type=Path, help="Explicit existing snapshot or canonical database; read-only"
    )
    parser.add_argument("--document-id", type=int, action="append", default=[])
    parser.add_argument("--cutoff-at", type=datetime.fromisoformat)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--run-id")
    parser.add_argument("--output-receipt", type=Path)
    args = parser.parse_args(argv)

    if args.db is not None:
        if args.cutoff_at is None:
            parser.error("--db requires an aware --cutoff-at and exactly12 --document-id values")
        try:
            retained = report_retained_sources(
                args.db,
                document_ids=tuple(args.document_id),
                cutoff_at=args.cutoff_at,
                repo_root=args.repo_root,
                run_id=args.run_id,
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            retained = {
                "status": "HOLD",
                "reason_codes": ["retained_evidence_verification_failed", type(exc).__name__],
            }
        rendered = json.dumps(retained, indent=2, sort_keys=True)
        if not args.check_only and args.output_receipt is not None:
            publish_text_no_clobber(args.output_receipt, rendered)
        print(rendered if args.json else f"Retained source evidence: {retained['status']}")
        # Successful byte verification is separately scoped; cost/readiness remains partial.
        return 0 if args.check_only and retained["status"] == "PARTIAL" else 1
    if args.document_id or args.cutoff_at or args.run_id or args.output_receipt:
        parser.error("selected-source options require --db")

    passed, errors, verified_count, total_bytes = verify_canary_corpus(args.manifest, args.fmp_dir)

    if not passed:
        sys.stderr.write(f"=== FMP Canary Corpus Verification FAILED ({len(errors)} errors) ===\n")
        for err in errors:
            sys.stderr.write(f"  x {err}\n")
        return 1

    if args.check_only:
        verification = {
            "status": "PASS",
            "scope": "manifest_file_hash_verification_only",
            "verified_corpus_files": verified_count,
            "total_corpus_bytes": total_bytes,
        }
        if args.json:
            print(json.dumps(verification, indent=2))
        else:
            print(f"Manifest file hashes verified: {verified_count} files, {total_bytes} bytes.")
        return 0

    receipt = run_cost_attribution(verified_count, total_bytes)
    if args.json:
        print(json.dumps(receipt, indent=2))
    else:
        print("Source-regime cost attribution: HOLD; measured run evidence unavailable.")
        print(f"Verified cache only: {verified_count} files, {total_bytes} bytes.")
        print(f"Receipt Path: {TMP_DIR / 'source_regime_cost_receipt.json'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
