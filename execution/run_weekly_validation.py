"""Weekly confidence backfill.

Rescores financial_facts.confidence / kpi_facts.confidence to fold fresh
validation-issue penalties into the per-fact score (idempotent).

The validation-engine SCAN (range / magnitude / cross-source checks that insert
validation_issues rows) runs DAILY as stage 3 of run_morning_pipeline
(run_validation_engine.py --gate), so it is NOT repeated here — this weekly task
used to re-run the identical full-population scan, inserting a duplicate set of
validation_issues every Sunday. apply_confidence_scores reads the unresolved
issues the daily run already inserted, so the rescore needs no scan of its own.

Runtime on the ~730k-row prod DB is a few seconds. The run is recorded in
ingestion_runs under directive "weekly_validation" so it surfaces in the
cron-health panel alongside the other scheduled rungs.

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
from pipeline.run_accounting import (  # noqa: E402
    PipelineRunSuppressedError,
    end_run,
    start_run,
    suppression_payload,
)

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
    run_id: str | None = None
    try:
        try:
            run_id = start_run(conn, directive="weekly_validation", ticker_scope=["ALL"])
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0

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
        if run_id is not None:
            end_run(conn, run_id, StageStatus.FAILED, error_summary=error_summary)
        print(f"ERROR: {error_summary}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
