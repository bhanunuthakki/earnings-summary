"""Weekly cross-source validation + confidence backfill.

Runs two stages in sequence:
1. validation engine — scans financial_facts / kpi_facts for range violations,
   magnitude jumps, and cross-source disagreements; inserts validation_issues rows.
2. confidence backfill — rescores financial_facts.confidence / kpi_facts.confidence
   to fold fresh issue penalties into the per-fact score (idempotent).

Runtime on the ~730k-row prod DB is ~12 s; no ticker batching is needed.
The run is recorded in ingestion_runs under directive "weekly_validation" so it
surfaces in the cron-health panel alongside the other scheduled rungs.

Usage:
    python execution/run_weekly_validation.py
    python execution/run_weekly_validation.py --db path/to/portfolio.db
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.runs import StageStatus  # noqa: E402
from pipeline.confidence import apply_confidence_scores  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import end_run, start_run  # noqa: E402
from pipeline.validation_engine import run_all_checks  # noqa: E402

_DB_DEFAULT = PROJECT_ROOT / "data" / "portfolio.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        "--db-path",
        dest="db",
        default=str(_DB_DEFAULT),
        help="Portfolio DB path (default: data/portfolio.db).",
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    run_id = start_run(conn, directive="weekly_validation", ticker_scope=["ALL"])

    try:
        t0 = time.monotonic()
        report = run_all_checks(conn, run_id=run_id, ticker=None)
        t_validation = time.monotonic() - t0

        total_issues = sum(o.issues_inserted for o in report.outcomes)

        t1 = time.monotonic()
        ff_outcome = apply_confidence_scores(conn, table="financial_facts", ticker=None, apply=True)
        kf_outcome = apply_confidence_scores(conn, table="kpi_facts", ticker=None, apply=True)
        t_backfill = time.monotonic() - t1

        end_run(conn, run_id, StageStatus.OK, error_summary=None)

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "stages": {
                        "validation_engine": {
                            "elapsed_s": round(t_validation, 1),
                            "issues_inserted": total_issues,
                            "by_rule": [
                                {
                                    "rule": o.rule.value,
                                    "rows_examined": o.rows_examined,
                                    "issues_inserted": o.issues_inserted,
                                }
                                for o in report.outcomes
                            ],
                        },
                        "confidence_backfill": {
                            "elapsed_s": round(t_backfill, 1),
                            "financial_facts": {
                                "examined": ff_outcome.examined,
                                "updated": ff_outcome.updated,
                            },
                            "kpi_facts": {
                                "examined": kf_outcome.examined,
                                "updated": kf_outcome.updated,
                            },
                        },
                    },
                },
                indent=2,
            )
        )
        return 0

    except Exception as exc:
        error_summary = f"{type(exc).__name__}: {exc}"[:500]
        end_run(conn, run_id, StageStatus.FAILED, error_summary=error_summary)
        print(f"ERROR: {error_summary}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
