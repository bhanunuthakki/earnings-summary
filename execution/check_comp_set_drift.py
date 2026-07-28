"""Weekly parity/QA drift check: bottoms-up industry/sector PE vs the FMP
snapshot reference (docs/design/comparable_sets_bottoms_up.md §7, Phase 2).

Flow (deterministic, no LLM, no network):
  1. Ingest the FMP `industry-pe-snapshot` / `sector-pe-snapshot` cache files
     (data/historical/sector_industry/, written by
     execution/save_fmp_data.py::run_sector_industry) into
     comp_set_metrics_daily as scope_type='fmp_snapshot' rows (§6 note —
     same-table join, vendor_field locators).
  2. For each industry/sector with both a bottoms-up median-PE row (latest
     as_of_date <= --date) and a nearest-prior snapshot row, compute
     drift_pct = (bottoms_up - fmp) / fmp.
  3. |drift| > DRIFT_ALERT_THRESHOLD (25%) surfaces as a data-quality
     signal: a structured stderr warning + one validation_issues row
     (rule=source_disagreement, severity=WARN) carrying both universes'
     numbers so "explainable by composition" is a one-glance check. Never a
     hard failure — §7 documents expected deviations (FMP's global
     multi-thousand-name universe vs our ~270-name US-listed pool; TTM
     cutoff timing around earnings season).

Cadence: weekly (cron/check_comp_set_drift.task.xml — Sunday 12:15 local,
off the 03:00-05:00 PT morning-pipeline window and off the Sunday-morning
eval-rung slot). Idempotent: snapshot ingest upserts on the table's unique
constraint; drift is computed fresh from the table each run and persisted
nowhere else (§13: no dedicated drift table).

Usage:
    python execution/check_comp_set_drift.py
    python execution/check_comp_set_drift.py --date 2026-07-18 --scope industry
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comp_set_drift import (  # noqa: E402
    DRIFT_ALERT_THRESHOLD,
    DriftReport,
    compute_drift,
    load_fmp_pe_snapshot,
    upsert_fmp_snapshot_rows,
)
from compute.comparable_sets import METHOD_VERSION  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from models.validation import Severity, ValidationRule  # noqa: E402
from pipeline.kpi_persistence import record_validation_issue  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402


def _log(event: dict[str, object]) -> None:
    """One structured JSON event line to stderr (repo convention: never mix
    logs and data on stdout)."""
    sys.stderr.write(json.dumps(event, default=str) + "\n")


def _report_alerts(
    conn: sqlite3.Connection,
    run_id: str,
    report: DriftReport,
) -> int:
    """Emit the §7 data-quality signal for each beyond-threshold drift row:
    structured warning + a validation_issues row (source_disagreement, WARN).
    Returns the alert count."""
    n_alerts = 0
    for result in report.results:
        if not result.alert:
            continue
        n_alerts += 1
        payload = result.to_dict()
        payload["event"] = "comp_set_drift_alert"
        payload["threshold"] = DRIFT_ALERT_THRESHOLD
        _log(payload)
        record_validation_issue(
            conn,
            run_id=run_id,
            source_doc_id=None,
            ticker=None,
            severity=Severity.WARN,
            rule=ValidationRule.SOURCE_DISAGREEMENT,
            raw_value=f"{result.drift_pct:+.1%}",
            expected=(
                f"{result.scope_type}:{result.scope_key} bottoms-up median PE "
                f"{result.bottoms_up_median:.1f} ({result.n_valid}/{result.n_members} members, "
                f"{result.as_of_date.isoformat()}) vs FMP snapshot {result.fmp_median:.1f} "
                f"({result.fmp_as_of.isoformat()}, {result.snapshot_age_days}d old); "
                f"|drift| <= {DRIFT_ALERT_THRESHOLD:.0%} expected"
            ),
        )
    return n_alerts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="ISO date to check as-of (default: today); the bottoms-up side uses the "
        "latest scope rows on/before this date, the FMP side the nearest prior snapshot",
    )
    parser.add_argument(
        "--scope",
        choices=["industry", "sector", "both"],
        default="both",
        help="Which scope(s) to check (default: both)",
    )
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Root containing data/historical/sector_industry (override for validation)",
    )
    args = parser.parse_args()

    as_of = datetime.strptime(args.date, "%Y-%m-%d").date()
    repo_root = Path(args.repo_root)
    scopes = ["industry", "sector"] if args.scope == "both" else [args.scope]

    conn = open_db(args.db)
    try:
        run_id = start_run(
            conn,
            directive="check_comp_set_drift",
            ticker_scope=[],
            invocation_inputs={
                "as_of": as_of.isoformat(),
                "repo_root": str(repo_root.resolve()),
                "scopes": scopes,
            },
        )
        summary: dict[str, object] = {"run_id": run_id, "as_of": as_of.isoformat()}
        total_alerts = 0
        failed = False
        for scope in scopes:
            try:
                entries = load_fmp_pe_snapshot(repo_root, scope)
                n_ingested = upsert_fmp_snapshot_rows(conn, entries, METHOD_VERSION)
                report = compute_drift(conn, scope, as_of, METHOD_VERSION)
                n_alerts = _report_alerts(conn, run_id, report)
                total_alerts += n_alerts
                summary[scope] = {
                    "snapshot_rows_ingested": n_ingested,
                    "compared": len(report.results),
                    "skipped": report.skipped,
                    "alerts": n_alerts,
                    "comparison_date": (
                        report.as_of_date.isoformat() if report.as_of_date else None
                    ),
                    "results": [r.to_dict() for r in report.results],
                }
                record_stage(
                    conn,
                    run_id,
                    f"_drift_{scope}",
                    StageName.COMPARABLE_SET_RESOLVE,
                    StageStatus.OK,
                )
            except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
                failed = True
                summary[scope] = {"error": f"{type(e).__name__}: {e}"[:300]}
                record_stage(
                    conn,
                    run_id,
                    f"_drift_{scope}",
                    StageName.COMPARABLE_SET_RESOLVE,
                    StageStatus.FAILED,
                    error_msg=f"{type(e).__name__}: {e}"[:500],
                )
                _log({"event": "comp_set_drift_scope_failed", "scope": scope, "error": str(e)})

        summary["total_alerts"] = total_alerts
        end_run(
            conn,
            run_id,
            StageStatus.FAILED if failed else StageStatus.OK,
            error_summary="drift scope failed" if failed else None,
        )
        print(json.dumps(summary, indent=2, default=str))
        # Alerts are a QA signal for the owner, not a pipeline failure (§7);
        # only a hard error in a scope pass exits non-zero.
        return 1 if failed else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
