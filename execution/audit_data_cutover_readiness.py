"""Run the cutoff-pinned data cutover readiness audit without mutating state."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution.audit_evidence_integrity import (  # noqa: E402
    configure_read_only_audit_connection,
)
from provenance.integrity_audit import (  # noqa: E402
    CutoverAuditOptions,
    audit_cutover_readiness,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _event(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr)


def _cutoff(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "cutoff must be an ISO-8601 datetime with timezone"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("cutoff must include an explicit timezone")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only cutoff-pinned data cutover readiness audit"
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--cutoff-at", type=_cutoff, required=True)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--fetch-size", type=int, default=250)
    parser.add_argument("--cache-mib", type=int, default=256)
    parser.add_argument("--mmap-mib", type=int, default=1024)
    args = parser.parse_args(argv)
    options = CutoverAuditOptions(
        cutoff_at=args.cutoff_at,
        sample_limit=args.sample_limit,
        fetch_size=args.fetch_size,
    )
    _event(
        "data_cutover_readiness_audit_started",
        db_path=str(args.db_path),
        cutoff_at=args.cutoff_at.isoformat(),
        sample_limit=args.sample_limit,
        fetch_size=args.fetch_size,
        cache_mib=args.cache_mib,
        mmap_mib=args.mmap_mib,
    )
    conn = connect_sqlite(args.db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        configure_read_only_audit_connection(
            conn,
            cache_mib=args.cache_mib,
            mmap_mib=args.mmap_mib,
        )
        conn.execute("BEGIN")
        try:
            summary = audit_cutover_readiness(conn, options)
        finally:
            conn.rollback()
    finally:
        conn.close()
    print(summary.model_dump_json())
    _event(
        "data_cutover_readiness_audit_finished",
        cutoff_at=args.cutoff_at.isoformat(),
        gate_count=len(summary.coverage),
        finding_count=len(summary.findings),
        has_blockers=summary.has_blockers,
    )
    return 2 if summary.has_blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
