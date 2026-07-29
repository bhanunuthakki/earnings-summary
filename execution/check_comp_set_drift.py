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
from hashlib import sha256
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comp_set_drift import (  # noqa: E402
    DRIFT_ALERT_THRESHOLD,
    DriftReport,
    SnapshotEntry,
    compute_drift,
    latest_scope_date,
    load_fmp_pe_snapshot,
    upsert_fmp_snapshot_rows,
)
from compute.comparable_sets import METHOD_VERSION  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from models.validation import Severity, ValidationRule  # noqa: E402
from pipeline.kpi_persistence import record_validation_issue  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    PipelineRunSuppressedError,
    end_run,
    record_stage,
    start_run,
    suppression_payload,
)

_MAX_FINGERPRINT_ROWS = 20_000


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _entry_payload(entry: SnapshotEntry) -> dict[str, object]:
    return {
        "as_of": entry.as_of.isoformat(),
        "exchanges": list(entry.exchanges),
        "kind": entry.kind,
        "pe": entry.pe,
        "scope_key": entry.scope_key,
    }


def _snapshot_kind(method_flags: object) -> str | None:
    if not isinstance(method_flags, str) or not method_flags.strip():
        return None
    try:
        raw: object = json.loads(method_flags)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    flags = cast("dict[str, object]", raw)
    kind = flags.get("snapshot_kind")
    return kind if isinstance(kind, str) else None


def _drift_input_fingerprint(
    conn: sqlite3.Connection,
    *,
    scopes: list[str],
    as_of: date,
    entries_by_scope: dict[str, list[SnapshotEntry]],
) -> str:
    """Hash exactly the rows/files that determine ingest and drift output."""
    fingerprint_scopes: list[dict[str, object]] = []
    row_count = sum(len(entries) for entries in entries_by_scope.values())
    if row_count > _MAX_FINGERPRINT_ROWS:
        raise ValueError("FMP snapshot fingerprint exceeds bounded row budget")

    for scope in scopes:
        entries = entries_by_scope[scope]
        report_date = latest_scope_date(conn, scope, as_of, METHOD_VERSION)
        bottom_rows: list[dict[str, object]] = []
        selected_fmp: list[dict[str, object]] = []
        if report_date is not None:
            raw_bottom = conn.execute(
                "SELECT scope_key, value, n_members, n_valid "
                "FROM comp_set_metrics_daily "
                "WHERE scope_type = ? AND metric = 'pe_ttm' AND stat_type = 'median' "
                "AND method_version = ? AND as_of_date = ? ORDER BY scope_key",
                (scope, METHOD_VERSION, report_date.isoformat()),
            ).fetchall()
            row_count += len(raw_bottom)
            if row_count > _MAX_FINGERPRINT_ROWS:
                raise ValueError("drift fingerprint exceeds bounded row budget")

            loaded_by_key: dict[str, dict[str, SnapshotEntry]] = {}
            for entry in entries:
                if entry.as_of <= report_date:
                    loaded_by_key.setdefault(entry.scope_key, {})[entry.as_of.isoformat()] = entry

            for row in raw_bottom:
                scope_key = str(row["scope_key"])
                bottom_rows.append(
                    {
                        "n_members": int(row["n_members"]),
                        "n_valid": int(row["n_valid"]),
                        "scope_key": scope_key,
                        "value": row["value"],
                    }
                )
                candidates = conn.execute(
                    "SELECT as_of_date, value, method_flags "
                    "FROM comp_set_metrics_daily "
                    "WHERE scope_type = 'fmp_snapshot' AND scope_key = ? "
                    "AND metric = 'pe_ttm' AND stat_type = 'median' "
                    "AND method_version = ? AND as_of_date <= ? AND value IS NOT NULL "
                    "ORDER BY as_of_date DESC LIMIT ?",
                    (
                        scope_key,
                        METHOD_VERSION,
                        report_date.isoformat(),
                        _MAX_FINGERPRINT_ROWS + 1,
                    ),
                ).fetchall()
                row_count += len(candidates)
                if row_count > _MAX_FINGERPRINT_ROWS:
                    raise ValueError("drift fingerprint exceeds bounded row budget")

                by_date: dict[str, dict[str, object]] = {
                    str(candidate["as_of_date"]): {
                        "as_of": str(candidate["as_of_date"]),
                        "kind": _snapshot_kind(candidate["method_flags"]),
                        "value": candidate["value"],
                    }
                    for candidate in candidates
                }
                for loaded_date, entry in loaded_by_key.get(scope_key, {}).items():
                    by_date[loaded_date] = {
                        "as_of": loaded_date,
                        "kind": entry.kind,
                        "value": entry.pe,
                    }
                selected = next(
                    (
                        candidate
                        for _, candidate in sorted(by_date.items(), reverse=True)
                        if candidate["kind"] in (None, scope)
                    ),
                    None,
                )
                selected_fmp.append({"scope_key": scope_key, "row": selected})

        fingerprint_scopes.append(
            {
                "bottoms_up_rows": bottom_rows,
                "fmp_source_rows": [_entry_payload(entry) for entry in entries],
                "report_date": report_date.isoformat() if report_date is not None else None,
                "scope": scope,
                "selected_fmp_rows": selected_fmp,
            }
        )
    return _canonical_sha256(fingerprint_scopes)


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
        entries_by_scope = {scope: load_fmp_pe_snapshot(repo_root, scope) for scope in scopes}
        input_fingerprint = _drift_input_fingerprint(
            conn,
            scopes=scopes,
            as_of=as_of,
            entries_by_scope=entries_by_scope,
        )
        try:
            run_id = start_run(
                conn,
                directive="check_comp_set_drift",
                ticker_scope=[],
                invocation_inputs={
                    "as_of": as_of.isoformat(),
                    "drift_alert_threshold": DRIFT_ALERT_THRESHOLD,
                    "input_fingerprint": input_fingerprint,
                    "method_version": METHOD_VERSION,
                    "repo_root": str(repo_root.resolve()),
                    "scopes": scopes,
                },
                deduplicate_completed=True,
            )
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0
        summary: dict[str, object] = {"run_id": run_id, "as_of": as_of.isoformat()}
        total_alerts = 0
        failed = False
        for scope in scopes:
            try:
                entries = entries_by_scope[scope]
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
