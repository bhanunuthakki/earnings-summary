"""Run the database evidence-integrity auditor without mutating any state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _event(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr)


def configure_read_only_audit_connection(
    conn: sqlite3.Connection,
    *,
    cache_mib: int,
    mmap_mib: int,
) -> None:
    """Apply bounded, connection-local tuning for large read-only audits."""
    if cache_mib < 0 or mmap_mib < 0:
        raise ValueError("audit cache and mmap budgets must be non-negative")
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute(f"PRAGMA cache_size = -{cache_mib * 1024}")
    conn.execute(f"PRAGMA mmap_size = {mmap_mib * 1024 * 1024}")


def main(argv: list[str] | None = None) -> int:
    # Standalone execution adds src/ above; keep these imports after that setup.
    from provenance.integrity_audit import AuditOptions, audit_connection, exit_code
    from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

    parser = argparse.ArgumentParser(
        description="Read-only investor-grade evidence integrity audit"
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--skip-deep-sqlite-checks", action="store_true")
    parser.add_argument("--verify-bytes", action="store_true")
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument(
        "--content-root",
        type=Path,
        action="append",
        default=[],
        help="Additional explicit root containing file:// evidence blobs; repeatable",
    )
    parser.add_argument("--max-verify-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--cache-mib", type=int, default=256)
    parser.add_argument("--mmap-mib", type=int, default=1024)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--fact-drift-effective-at", type=datetime.fromisoformat)
    parser.add_argument("--fact-drift-known-at", type=datetime.fromisoformat)
    parser.add_argument("--fact-drift-only", action="store_true")
    args = parser.parse_args(argv)
    options = AuditOptions(
        sample_limit=args.sample_limit,
        deep_sqlite_checks=not args.skip_deep_sqlite_checks,
        verify_bytes=args.verify_bytes,
        repo_root=args.repo_root,
        content_roots=tuple(args.content_root),
        max_verify_bytes=args.max_verify_bytes,
        fact_drift_effective_at=args.fact_drift_effective_at,
        fact_drift_known_at=args.fact_drift_known_at,
        fact_drift_only=args.fact_drift_only,
    )
    _event(
        "evidence_integrity_audit_started",
        db_path=str(args.db_path),
        deep_sqlite_checks=not args.skip_deep_sqlite_checks,
        verify_bytes=args.verify_bytes,
        content_roots=[
            str(root)
            for root in (() if args.repo_root is None else (args.repo_root,))
            + tuple(args.content_root)
        ],
        cache_mib=args.cache_mib,
        mmap_mib=args.mmap_mib,
        fact_drift_mode=("drift_only" if args.fact_drift_only else "full"),
        fact_drift_effective_at=(
            None
            if args.fact_drift_effective_at is None
            else args.fact_drift_effective_at.isoformat()
        ),
        fact_drift_known_at=(
            None if args.fact_drift_known_at is None else args.fact_drift_known_at.isoformat()
        ),
    )
    conn = connect_sqlite(args.db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        configure_read_only_audit_connection(
            conn,
            cache_mib=args.cache_mib,
            mmap_mib=args.mmap_mib,
        )
        summary = audit_connection(conn, options)
    finally:
        conn.close()
    print(summary.model_dump_json())
    _event(
        "evidence_integrity_audit_finished",
        finding_count=len(summary.findings),
        has_blockers=summary.has_blockers,
    )
    return exit_code(has_blockers=summary.has_blockers, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
