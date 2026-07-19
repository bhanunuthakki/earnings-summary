"""Compute + persist one day's bottoms-up comparable-set metrics.

docs/design/comparable_sets_bottoms_up.md §5/§8. Phase 1 shipped
`scope_type='comparable_set'` rows for `--all-portfolio` subjects, no
`--backfill` (§9: "Phase 1 ships forward-only ... no historical backfill" — a
`--backfill` flag on top of the current, single frozen membership is a
Phase 2 addition, still not built here — deferred, see docstring note below).

Phase 2 (§11) adds two things:
  1. `--all-tracked` widens subject resolution to portfolio+watchlist+
     evaluation (`pipeline.queries.ANALYZED_LIST_TYPES`), mirroring
     `build_comparable_sets.py`'s Phase 2 widen.
  2. Every run also computes `scope_type='industry'`/`'sector'` rows (§4.1's
     "multiples benchmark") for every industry/sector present in the pool —
     unconditionally, since this is pure deterministic math with no
     incremental cost concern (`--skip-industry-sector` opts out for a
     narrow/fast debug run). These rows use the pool-wide slice from
     `compute.comparable_sets.resolve_industry_scope_members`/
     `resolve_sector_scope_members` (§6: "not a per-ticker market-cap band")
     and are keyed `scope_key=<industry or sector string>` with the SAME
     `comp_set_metrics_daily` table and unique constraint as
     `scope_type='comparable_set'` rows — no new table, per §6/§13.

Usage:
    python execution/track_comp_metrics.py --date 2026-07-17
    python execution/track_comp_metrics.py --ticker NU --date 2026-07-17
    python execution/track_comp_metrics.py --all-tracked --date 2026-07-17
    python execution/track_comp_metrics.py --skip-industry-sector --ticker NU

Requires each subject to already have a frozen comparable set (run
execution/build_comparable_sets.py first) — a subject with no comparable_sets
row is logged and skipped, not treated as an error (it just hasn't been
built yet). Industry/sector scope rows have no such prerequisite (they read
the pool directly, not a frozen comparable_sets row).

Idempotent via the comp_set_metrics_daily unique constraint (scope_type,
scope_key, as_of_date, metric, stat_type, method_version) — re-running the
same date upserts (refreshes value/coverage/computed_at in case a
late-arriving cache file changes the answer), per §8.
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

from compute.comp_set_metrics import (  # noqa: E402
    MetricRow,
    compute_comparable_set_metrics,
    upsert_metric_rows,
)
from compute.comparable_sets import (  # noqa: E402
    METHOD_VERSION,
    ComparableSetMember,
    comparable_set_id,
    load_pool,
    pool_industries,
    pool_sectors,
    resolve_industry_scope_members,
    resolve_sector_scope_members,
)
from compute.comparable_sets import metric_class as classify_metric_class  # noqa: E402
from models.companies import ListType  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.queries import ANALYZED_LIST_TYPES, open_db, tracked_companies_for_user  # noqa: E402
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    list_types = ANALYZED_LIST_TYPES if args.all_tracked else frozenset({ListType.PORTFOLIO})
    companies = tracked_companies_for_user(conn, only_classified=False, list_types=list_types)
    return sorted({c.ticker for c in companies})


def _load_frozen_set(
    conn: sqlite3.Connection, ticker: str
) -> tuple[str, list[tuple[str, str, bool]]] | None:
    """(metric_class, [(member_ticker, membership_reason, context_only), ...])
    for the ticker's currently-open comparable set, or None if never built."""
    set_id = comparable_set_id(ticker, METHOD_VERSION)
    cur = conn.execute(
        "SELECT metric_class FROM comparable_sets WHERE comparable_set_id = ?", (set_id,)
    )
    row = cur.fetchone()
    if row is None:
        return None
    metric_class = str(row["metric_class"])
    cur = conn.execute(
        "SELECT member_ticker, membership_reason, context_only FROM comparable_set_members "
        "WHERE comparable_set_id = ? AND valid_to IS NULL",
        (set_id,),
    )
    members = [
        (str(r["member_ticker"]), str(r["membership_reason"]), bool(r["context_only"]))
        for r in cur.fetchall()
    ]
    return metric_class, members


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="ISO date to compute metrics as-of (default: today)",
    )
    parser.add_argument("--ticker", help="Single subject ticker (default: all portfolio subjects)")
    parser.add_argument(
        "--all-tracked",
        action="store_true",
        help="Subject scope = portfolio+watchlist+evaluation instead of portfolio-only "
        "(Phase 2, §11; ignored when --ticker is given)",
    )
    parser.add_argument(
        "--skip-industry-sector",
        action="store_true",
        help="Skip the scope_type='industry'/'sector' pass (Phase 2, §4.1) — "
        "comparable_set-scope subject rows only",
    )
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Root containing data/historical/fmp (override for read-only validation)",
    )
    args = parser.parse_args()

    as_of = datetime.strptime(args.date, "%Y-%m-%d").date()
    repo_root = Path(args.repo_root)
    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(conn, args)
        if not tickers:
            sys.stderr.write("No tickers resolved; nothing to do\n")
            return 0

        run_id = start_run(conn, directive="track_comp_metrics", ticker_scope=tickers)
        results: list[dict[str, object]] = []
        skipped = 0
        failed = 0
        for ticker in tickers:
            frozen = _load_frozen_set(conn, ticker)
            if frozen is None:
                record_stage(
                    conn,
                    run_id,
                    ticker,
                    StageName.COMPARABLE_SET_RESOLVE,
                    StageStatus.SKIPPED,
                    error_msg="no frozen comparable_sets row — run build_comparable_sets.py first",
                )
                results.append({"ticker": ticker, "skipped_reason": "no frozen comparable set"})
                skipped += 1
                continue
            metric_class, member_tuples = frozen
            members = [
                ComparableSetMember(member_ticker=t, membership_reason=r, context_only=c)
                for t, r, c in member_tuples
            ]
            try:
                rows: list[MetricRow] = compute_comparable_set_metrics(
                    repo_root, members, metric_class, as_of
                )
                set_id = comparable_set_id(ticker, METHOD_VERSION)
                n_written = upsert_metric_rows(
                    conn, "comparable_set", set_id, as_of, METHOD_VERSION, rows
                )
                record_stage(conn, run_id, ticker, StageName.COMPARABLE_SET_RESOLVE, StageStatus.OK)
                results.append(
                    {
                        "ticker": ticker,
                        "comparable_set_id": set_id,
                        "rows_written": n_written,
                        "metrics": {
                            f"{r.metric}:{r.stat_type}": {
                                "value": r.value,
                                "coverage_pct": round(r.coverage_pct, 3),
                                "n_valid": r.n_valid,
                                "n_members": r.n_members,
                                "method_flags": r.method_flags,
                            }
                            for r in rows
                        },
                    }
                )
            except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
                record_stage(
                    conn,
                    run_id,
                    ticker,
                    StageName.COMPARABLE_SET_RESOLVE,
                    StageStatus.FAILED,
                    error_msg=f"{type(e).__name__}: {e}"[:500],
                )
                failed += 1
                sys.stderr.write(f"FAILED {ticker}: {type(e).__name__}: {e}\n")

        scope_results: list[dict[str, object]] = []
        if not args.skip_industry_sector:
            pool = load_pool(conn, repo_root)
            for scope_type, keys, resolve_members in (
                ("industry", pool_industries(pool), resolve_industry_scope_members),
                ("sector", pool_sectors(pool), resolve_sector_scope_members),
            ):
                for key in keys:
                    stage_ticker = f"__{scope_type}__:{key}"
                    member_tickers = resolve_members(pool, key)
                    mclass = classify_metric_class(
                        key if scope_type == "sector" else None,
                        key if scope_type == "industry" else None,
                    )
                    members = [
                        ComparableSetMember(member_ticker=t, membership_reason="pool_slice")
                        for t in member_tickers
                    ]
                    try:
                        rows = compute_comparable_set_metrics(repo_root, members, mclass, as_of)
                        n_written = upsert_metric_rows(
                            conn, scope_type, key, as_of, METHOD_VERSION, rows
                        )
                        record_stage(
                            conn,
                            run_id,
                            stage_ticker,
                            StageName.COMPARABLE_SET_RESOLVE,
                            StageStatus.OK,
                        )
                        scope_results.append(
                            {
                                "scope_type": scope_type,
                                "scope_key": key,
                                "n_members": len(member_tickers),
                                "rows_written": n_written,
                            }
                        )
                    except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
                        record_stage(
                            conn,
                            run_id,
                            stage_ticker,
                            StageName.COMPARABLE_SET_RESOLVE,
                            StageStatus.FAILED,
                            error_msg=f"{type(e).__name__}: {e}"[:500],
                        )
                        failed += 1
                        sys.stderr.write(f"FAILED {stage_ticker}: {type(e).__name__}: {e}\n")

        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        end_run(
            conn, run_id, terminal, error_summary=f"{failed} tickers failed" if failed else None
        )

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "as_of": as_of.isoformat(),
                    "tickers": len(tickers),
                    "skipped": skipped,
                    "failed": failed,
                    "results": results,
                    "scope_results": scope_results,
                },
                indent=2,
                default=str,
            )
        )
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
