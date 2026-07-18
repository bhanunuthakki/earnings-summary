"""Daily bottoms-up comp-set aggregate computation (docs/design/
comparable_sets_bottoms_up.md sections 5, 8). Phase 1 scope: ``scope_type=
'comparable_set'`` rows only — pool-wide industry/sector scope rows and the
``--backfill`` flag are Phase 2 (section 9: Phase 1 ships forward-only, no
historical backfill, so every row's membership and computed value both belong to
the date they claim to).

Iterates every currently-frozen comparable set (whatever ``build_comparable_sets.py``
has resolved) and computes/upserts its PE / EV-EBITDA / P-B / P-TBV / rev-YoY /
FCF-yield median+aggregate rows for one date. Idempotent via the ``(scope_type,
scope_key, as_of_date, metric, stat_type, method_version)`` unique constraint —
re-running an already-written date upserts (refreshes) rather than duplicating.

Usage:
    python execution/track_comp_metrics.py --date 2026-07-17
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comp_set_metrics import (  # noqa: E402
    compute_metrics_for_set,
    load_member_financials,
    persist_metrics_daily,
)
from compute.comparable_sets import (  # noqa: E402
    active_members,
    get_method_flags,
    open_comparable_sets,
)
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date", default=None, help="ISO date to compute (default: today, local time)"
    )
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    as_of = date.fromisoformat(args.date) if args.date else date.today()
    fmp_dir = PROJECT_ROOT / "data" / "historical" / "fmp"

    conn = open_db(args.db)
    try:
        sets = open_comparable_sets(conn)
        if not sets:
            print(json.dumps({"warning": "no comparable sets resolved yet"}, indent=2))
            return 0

        run_id = start_run(conn, directive="track_comp_metrics", ticker_scope=[s[1] for s in sets])
        per_set: list[dict[str, object]] = []
        failed = 0

        for comparable_set_id, ticker, metric_class, method_version in sets:
            try:
                members = active_members(conn, comparable_set_id)
                financials = [
                    load_member_financials(
                        fmp_dir, member_ticker, context_only=context_only, as_of=as_of
                    )
                    for member_ticker, _reason, context_only in members
                ]
                method_flags = get_method_flags(conn, comparable_set_id)
                results = compute_metrics_for_set(
                    financials, metric_class, method_flags_passthrough=method_flags
                )
                rows_written = persist_metrics_daily(
                    conn,
                    scope_type="comparable_set",
                    scope_key=comparable_set_id,
                    as_of_date=as_of,
                    results=results,
                    method_version=method_version,
                )
            except (ValueError, OSError) as e:
                failed += 1
                record_stage(
                    conn,
                    run_id,
                    ticker,
                    StageName.COMPUTE,
                    StageStatus.FAILED,
                    error_msg=f"{type(e).__name__}: {e}"[:500],
                )
                sys.stderr.write(
                    json.dumps({"event": "compute_failed", "ticker": ticker, "error": str(e)})
                    + "\n"
                )
                continue

            record_stage(conn, run_id, ticker, StageName.COMPUTE, StageStatus.OK)
            per_set.append(
                {
                    "comparable_set_id": comparable_set_id,
                    "ticker": ticker,
                    "n_members": len(members),
                    "rows_written": rows_written,
                }
            )

        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        end_run(conn, run_id, terminal, error_summary=f"{failed} failed" if failed else None)

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "as_of_date": as_of.isoformat(),
                    "sets_processed": len(per_set),
                    "failed": failed,
                    "per_set": per_set,
                },
                indent=2,
            )
        )
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
