"""Match legacy CompanyFacts rows to immutable accession-scoped evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.sec_companyfacts_fact_matcher import (  # noqa: E402
    CompanyFactsFactMatcherRequest,
    emit_structured_event,
    match_legacy_companyfacts_evidence,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Explicit isolated SQLite target; data/portfolio.db is rejected",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        required=True,
        help="Explicit immutable CompanyFacts blob root",
    )
    parser.add_argument(
        "--checkpoint-root",
        type=Path,
        required=True,
        help="Explicit checkpoint root; checkpoints never drive readiness",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--task-id",
        default="legacy-companyfacts-fact-match",
    )
    parser.add_argument(
        "--fact-table",
        action="append",
        choices=("financial_facts", "kpi_facts"),
        default=[],
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Append bounded match revisions (facts and observations stay unchanged)",
    )
    parser.add_argument(
        "--include-items",
        action="store_true",
        help="Include per-fact details on stdout; limited to batches of 100",
    )
    args = parser.parse_args(argv)
    if args.include_items and args.batch_size > 100:
        parser.error("--include-items requires --batch-size <= 100")

    db_path = args.db.resolve()
    live_path = (PROJECT_ROOT / "data" / "portfolio.db").resolve()
    if db_path == live_path:
        emit_structured_event(
            "legacy_companyfacts_fact_match_live_db_rejected",
            db_path=str(db_path),
        )
        return 2
    request = CompanyFactsFactMatcherRequest(
        blob_root=args.blob_root,
        checkpoint_root=args.checkpoint_root,
        apply=args.apply,
        batch_size=args.batch_size,
        task_id=args.task_id,
        fact_tables=(
            tuple(args.fact_table) if args.fact_table else ("financial_facts", "kpi_facts")
        ),
    )
    role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
    try:
        conn = connect_sqlite(
            db_path,
            role=role,
            schema_preflight=request.apply,
        )
        try:
            summary = match_legacy_companyfacts_evidence(conn, request)
        finally:
            conn.close()
    except Exception as exc:
        emit_structured_event(
            "legacy_companyfacts_fact_match_failed",
            error_type=type(exc).__name__,
            message=redact(exc),
        )
        return 1
    emit_structured_event(
        "legacy_companyfacts_fact_match_completed",
        mode=summary.mode,
        considered=summary.considered,
        accepted=summary.accepted,
        retryable=summary.retryable,
        terminal=summary.terminal,
    )
    payload = summary.model_dump(mode="json")
    if not args.include_items:
        payload["items"] = []
        payload["items_omitted"] = summary.considered
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
