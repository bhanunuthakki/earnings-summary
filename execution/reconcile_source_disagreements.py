"""Triage the open source_disagreement validation issues + audit reader tiering.

Two jobs over the ~162k FMP+SEC duplicated financial_facts keys the EDGAR
backfill created (see pipeline.reader_tier_audit):

  --reconcile  Auto-resolve OPEN source_disagreement issues whose relative delta
               is <= --threshold-pct (default 1.0) and that name SEC on one side,
               in SEC's favor (the tier-aware readers already serve the SEC row
               post the reader-tier-hardening PR). Larger deltas are left OPEN and
               their residual count is what the Provenance console surfaces.

  --audit      Sample --limit duplicated keys and write a READER_TIER_MISMATCH
               validation issue for any key where the financials reader
               materializes a value other than the canonical (source_quality_tier,
               id) winner — a regression tripwire, wrapped in an ingestion_runs row.

Run at least one of --reconcile / --audit (both allowed). Take a fresh
sqlite-backup copy of prod BEFORE running with writes; --dry-run reports without
touching the DB.

Usage:
    python execution/reconcile_source_disagreements.py --reconcile
    python execution/reconcile_source_disagreements.py --reconcile --dry-run
    python execution/reconcile_source_disagreements.py --audit --limit 300
    python execution/reconcile_source_disagreements.py --reconcile --audit
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.runs import StageStatus  # noqa: E402
from pipeline.invocation_fingerprint import payload_sha256  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.reader_tier_audit import (  # noqa: E402
    DEFAULT_RECONCILE_THRESHOLD_PCT,
    audit_readers,
    open_source_disagreement_count,
    reconcile_source_disagreements,
    sample_duplicated_keys,
)
from pipeline.run_accounting import (  # noqa: E402
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    start_run,
    suppression_payload,
)


def _audit_snapshot_sha256(
    conn: sqlite3.Connection,
    *,
    limit: int,
    ticker: str | None,
) -> str:
    keys = sample_duplicated_keys(conn, limit=limit, ticker=ticker)
    rows: list[JsonValue] = []
    for key in keys:
        rows.append(
            {
                "key": {
                    "ticker": key.ticker,
                    "period_end": key.period_end,
                    "fiscal_period_type": key.fiscal_period_type,
                    "line_item": key.line_item,
                },
                "candidates": list(key.candidate_snapshot),
            }
        )
    return payload_sha256(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        "--db-path",
        dest="db",
        default=str(PROJECT_ROOT / "data" / "portfolio.db"),
        help="Portfolio DB path.",
    )
    parser.add_argument(
        "--reconcile", action="store_true", help="Auto-resolve near-agreement disagreements."
    )
    parser.add_argument(
        "--audit", action="store_true", help="Sample duplicated keys and flag reader-vs-tier drift."
    )
    parser.add_argument("--ticker", default=None, help="Restrict --audit to one ticker.")
    parser.add_argument(
        "--limit", type=int, default=200, help="Max duplicated keys to sample for --audit."
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=DEFAULT_RECONCILE_THRESHOLD_PCT,
        help="Relative-delta cutoff (percent) for auto-resolving in SEC's favor.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.reconcile and not args.audit:
        print("Nothing to do: pass --reconcile and/or --audit.", file=sys.stderr)
        return 1

    conn = open_db(args.db)
    out: dict[str, object] = {"db": args.db, "dry_run": args.dry_run}
    try:
        if args.reconcile:
            r = reconcile_source_disagreements(
                conn, threshold_pct=args.threshold_pct, dry_run=args.dry_run
            )
            out["reconcile"] = {
                "examined": r.examined,
                "auto_resolved": r.auto_resolved,
                "left_open": r.left_open,
                "unparsed": r.unparsed,
                "threshold_pct": args.threshold_pct,
            }

        if args.audit:
            scope = [args.ticker.upper()] if args.ticker else ["ALL"]
            run_id = (
                "reader_tier_audit_dry_run"
                if args.dry_run
                else start_run(
                    conn,
                    directive="reader_tier_audit",
                    ticker_scope=scope,
                    invocation_inputs={
                        "audit": bool(args.audit),
                        "reconcile": bool(args.reconcile),
                        "ticker": args.ticker.upper() if args.ticker else None,
                        "limit": args.limit,
                        "threshold_pct": args.threshold_pct,
                        "dry_run": bool(args.dry_run),
                        "audit_snapshot_sha256": _audit_snapshot_sha256(
                            conn,
                            limit=args.limit,
                            ticker=args.ticker,
                        ),
                    },
                    deduplicate_completed=True,
                )
            )
            a = audit_readers(
                conn,
                run_id=run_id,
                db_path=Path(args.db),
                limit=args.limit,
                ticker=args.ticker,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                end_run(conn, run_id, StageStatus.OK, error_summary=None)
            out["audit"] = {
                "run_id": run_id,
                "keys_examined": a.keys_examined,
                "mismatches": a.mismatches,
                "issues_written": a.issues_written,
                "sample_mismatches": [
                    {
                        "ticker": t,
                        "line_item": li,
                        "period_end": pe,
                        "tier_winner": cv,
                        "reader_value": rv,
                    }
                    for t, li, pe, cv, rv in a.detail[:10]
                ],
            }

        out["open_source_disagreements"] = open_source_disagreement_count(conn)
        print(json.dumps(out, indent=2))
        return 0
    except PipelineRunSuppressedError as exc:
        print(json.dumps(suppression_payload(exc)))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
