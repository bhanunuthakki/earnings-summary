"""Parity/QA drift check: bottoms-up industry/sector PE vs FMP's snapshot.

docs/design/comparable_sets_bottoms_up.md §7. Phase 2 CLI (§11). Weekly
cadence (mirrors the existing Sunday eval-rung slot — no LLM leg, so it
doesn't need to respect the 03:00-05:00 PT protected windows registered in
directives/llm_quota_scheduling.md, but is scheduled clear of that window
anyway, as a matter of hygiene, per the doc's own instruction).

Logic (§7): for each pool industry/sector with both a bottoms-up
`scope_type='industry'`/`'sector'` row (written by
execution/track_comp_metrics.py, metric='pe_ttm', stat_type='median') and an
FMP reference value on the same-or-nearest-prior `as_of_date`, compute
    drift_pct = (bottoms_up_median - fmp_median) / fmp_median
and flag anything beyond `DRIFT_ALERT_THRESHOLD` (25%) as a data-quality
signal — never a hard failure (§7: "a QA signal for the owner, not a
pipeline-breaking assertion").

Deviation from the doc's literal §6/§7 wording, noted per Directive
Maintenance convention: the doc describes the FMP side as
`scope_type='fmp_snapshot'` rows living IN `comp_set_metrics_daily` (so the
drift check is "a same-table join, not a cross-format comparison"), but no
CLI in §8 (Phase 1 or Phase 2) ever writes those rows — `save_fmp_data.py::
run_sector_industry` writes the raw FMP snapshot to
`data/historical/sector_industry/{industry,sector}_pe_snapshot.json` (§0's
"the FMP snapshot still earns its keep as the QA drift-check's independent
reference") and nothing re-keys it into `comp_set_metrics_daily`. Rather than
invent a THIRD CLI whose only job is copying FMP's own already-cached JSON
into a DB row (out of scope for either Phase 1 or Phase 2's CLI list), this
script reads those two JSON files directly at compare-time and diffs them
in-memory against our own `comp_set_metrics_daily` rows. This keeps the
"prefer no new table" spirit of §7's own note (a dedicated
`comp_set_drift_checks` table was already ruled out there) and doesn't
require a job neither phase specifies. Verified in production data
(2026-07-18): both files are a single row per industry/sector, all
`exchange="NASDAQ"`, one `date` for the whole file (a point-in-time snapshot,
not a daily series) — so "nearest prior" only ever matters on our own side.

Per AGENTS.md's structured-logging convention, one JSON line per event is
written to stderr for every flagged drift; stdout carries only the final
summary JSON. No `ingestion_runs`/`stage_transitions` wrapping (unlike
build_comparable_sets.py / track_comp_metrics.py) — this script performs no
database writes (§7's "prefer querying directly" choice means there is
nothing here for run-accounting to audit; it only reads two tables/files and
prints a report).

Usage:
    python execution/check_comp_set_drift.py
    python execution/check_comp_set_drift.py --date 2026-07-17
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comparable_sets import METHOD_VERSION  # noqa: E402
from pipeline.queries import open_db  # noqa: E402

# Beyond this, without an explainable composition reason (universe size/TTM
# cutoff timing — both documented in §7 as expected, not bugs), a drift is
# surfaced as a data-quality flag. Named, greppable constant per §7.
DRIFT_ALERT_THRESHOLD = 0.25


def _log_event(event: str, **fields: object) -> None:
    """One structured JSON line per event to stderr (AGENTS.md logging
    convention — never mixed with the stdout report)."""
    sys.stderr.write(json.dumps({"event": event, **fields}, default=str) + "\n")


def _load_fmp_snapshot(
    repo_root: Path, filename: str, key_field: str
) -> dict[str, tuple[float, str]]:
    """Read `data/historical/sector_industry/{filename}` -> {key: (pe, date)}.

    Missing/unreadable file degrades to an empty dict (logged), not an
    exception — the FMP snapshot is a QA reference, not a hard dependency
    (§0/§7: the snapshot may simply never have been fetched)."""
    path = repo_root / "data" / "historical" / "sector_industry" / filename
    if not path.exists():
        _log_event("comp_set_drift_snapshot_missing", path=str(path))
        return {}
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log_event("comp_set_drift_snapshot_unreadable", path=str(path), error=str(e))
        return {}
    if not isinstance(raw, list):
        return {}
    rows = cast("list[object]", raw)
    out: dict[str, tuple[float, str]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = cast("dict[str, object]", r)
        key = row.get(key_field)
        pe = row.get("pe")
        row_date = row.get("date")
        if (
            isinstance(key, str)
            and key
            and isinstance(pe, (int, float))
            and isinstance(row_date, str)
            and key not in out  # first occurrence wins (one row per key expected)
        ):
            out[key] = (float(pe), row_date)
    return out


def _load_bottoms_up(
    conn: sqlite3.Connection, scope_type: str, as_of: date, method_version: int
) -> dict[str, tuple[float | None, str, int, int, float]]:
    """{scope_key: (value, as_of_date, n_members, n_valid, coverage_pct)} for
    the nearest row on/before `as_of` per scope_key, scope_type='industry'/
    'sector', metric='pe_ttm', stat_type='median' (§7: "the drift check only
    ever compares FMP's median-shaped number against our `median` row, never
    `aggregate`")."""
    cur = conn.execute(
        "SELECT scope_key, value, as_of_date, n_members, n_valid, coverage_pct "
        "FROM comp_set_metrics_daily "
        "WHERE scope_type = ? AND metric = 'pe_ttm' AND stat_type = 'median' "
        "AND method_version = ? AND as_of_date <= ? "
        "ORDER BY scope_key, as_of_date DESC",
        (scope_type, method_version, as_of.isoformat()),
    )
    out: dict[str, tuple[float | None, str, int, int, float]] = {}
    for row in cur.fetchall():
        key = str(row["scope_key"])
        if key in out:
            continue  # first row per key (DESC-ordered) = nearest prior-or-equal date
        value = row["value"]
        out[key] = (
            float(value) if value is not None else None,
            str(row["as_of_date"]),
            int(row["n_members"]),
            int(row["n_valid"]),
            float(row["coverage_pct"]),
        )
    return out


def _compare_scope(
    conn: sqlite3.Connection,
    scope_type: str,
    fmp_snapshot: dict[str, tuple[float, str]],
    as_of: date,
    method_version: int,
) -> list[dict[str, object]]:
    bottoms_up = _load_bottoms_up(conn, scope_type, as_of, method_version)
    results: list[dict[str, object]] = []
    for key, (bu_value, bu_as_of, n_members, n_valid, coverage_pct) in sorted(bottoms_up.items()):
        fmp = fmp_snapshot.get(key)
        if fmp is None or bu_value is None:
            continue
        fmp_pe, fmp_date = fmp
        if fmp_pe == 0:
            continue  # undefined ratio, not a drift
        drift_pct = (bu_value - fmp_pe) / fmp_pe
        flagged = abs(drift_pct) > DRIFT_ALERT_THRESHOLD
        row: dict[str, object] = {
            "scope_type": scope_type,
            "scope_key": key,
            "bottoms_up_median": bu_value,
            "bottoms_up_as_of": bu_as_of,
            "bottoms_up_n_members": n_members,
            "bottoms_up_n_valid": n_valid,
            "bottoms_up_coverage_pct": round(coverage_pct, 3),
            "fmp_median": fmp_pe,
            "fmp_as_of": fmp_date,
            "drift_pct": round(drift_pct, 4),
            "flagged": flagged,
        }
        results.append(row)
        if flagged:
            _log_event("comp_set_drift_flag", **row)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="ISO date to check drift as-of (default: today; each scope_key uses its own "
        "nearest-prior-or-equal bottoms-up row)",
    )
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Root containing data/historical/sector_industry (override for read-only validation)",
    )
    args = parser.parse_args()

    as_of = datetime.strptime(args.date, "%Y-%m-%d").date()
    repo_root = Path(args.repo_root)
    conn = open_db(args.db)
    try:
        industry_snapshot = _load_fmp_snapshot(repo_root, "industry_pe_snapshot.json", "industry")
        sector_snapshot = _load_fmp_snapshot(repo_root, "sector_pe_snapshot.json", "sector")

        industry_results = _compare_scope(
            conn, "industry", industry_snapshot, as_of, METHOD_VERSION
        )
        sector_results = _compare_scope(conn, "sector", sector_snapshot, as_of, METHOD_VERSION)

        all_results = industry_results + sector_results
        flagged = [r for r in all_results if r["flagged"]]

        print(
            json.dumps(
                {
                    "as_of": as_of.isoformat(),
                    "drift_alert_threshold": DRIFT_ALERT_THRESHOLD,
                    "checked": len(all_results),
                    "flagged_count": len(flagged),
                    "industry": industry_results,
                    "sector": sector_results,
                },
                indent=2,
                default=str,
            )
        )
        # Never a hard failure (§7) -- this is a QA signal for the owner, not
        # a pipeline-breaking assertion. Exit 0 regardless of flags found.
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
