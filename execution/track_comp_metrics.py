"""Compute + persist one day's bottoms-up comparable-set metrics.

docs/design/comparable_sets_bottoms_up.md §5/§6/§8. Phase 1 shipped
`scope_type='comparable_set'` rows for portfolio subjects; Phase 2 (§11)
adds:
  - `--all-tracked` subject widening (portfolio+watchlist+evaluation);
  - pool-wide `scope_type='industry'` / `'sector'` slice rows (§6 — every
    US-listed pool member in that industry/sector, no per-subject market-cap
    band), computed on every run without `--ticker`: subject-independent
    pure local math over the same cache files, and the §4.1 multiples
    benchmark the ETF proxy explicitly does NOT provide.

Still forward-only: `--backfill` (§9) remains unbuilt — nothing consumes
the longer trend line yet, and the honest-labeling machinery it needs
(membership_asof) should land with its first real consumer.

Usage:
    python execution/track_comp_metrics.py --date 2026-07-17
    python execution/track_comp_metrics.py --all-tracked --date 2026-07-17
    python execution/track_comp_metrics.py --ticker NU --date 2026-07-17

Requires each subject to already have a frozen comparable set (run
execution/build_comparable_sets.py first) — a subject with no comparable_sets
row is logged and skipped, not treated as an error (it just hasn't been
built yet).

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
from hashlib import sha256
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
    pool_scope_slices,
    scope_metric_class,
)
from models.companies import ListType  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.queries import open_db, tracked_companies_for_user  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    PipelineRunSuppressedError,
    end_run,
    record_stage,
    start_run,
    suppression_payload,
)

# Phase 2 (§11): subjects widen to watchlist+evaluation with --all-tracked;
# `index_member` names stay pool-candidates only, never subjects.
TRACKED_SUBJECT_LIST_TYPES = frozenset(
    {ListType.PORTFOLIO, ListType.WATCHLIST, ListType.EVALUATION}
)

# stage_transitions "ticker" tag for the pool-wide slice pass (one stage row
# summarizes the whole pass; per-slice detail lives in the stdout JSON).
POOL_SCOPES_STAGE_TICKER = "_pool_scopes"
_MEMBER_SOURCE_SUFFIXES: tuple[str, ...] = (
    "balance_sheet_quarterly",
    "historical_market_cap",
    "income_statement_quarterly",
    "key_metrics_quarterly",
)
_MAX_FINGERPRINT_MEMBERSHIPS = 20_000
# Phase 2 (§6) fingerprints every pool-slice member's cache files, not just the
# portfolio subjects' — ~1,440 US-listed slice tickers x 4 suffixes ≈ 5.8k files
# today and it tracks pool growth, so the Phase-1-era 5,000 ceiling tripped.
# Sized for parity with the membership budget (~3x current headroom).
_MAX_FINGERPRINT_FILES = 20_000

# Fingerprint scheme tag, hashed into the payload below. Reading all ~5.8k files
# to SHA-256 their contents cost ~74s over ~1.0 GB at current pool scale (vs
# ~0.4s to stat them) on a job that runs daily, so a source file is identified
# by (st_mtime_ns, st_size) instead. The trade: a rewrite preserving BOTH size
# and mtime is invisible here. These caches are only ever rewritten wholesale by
# a refetch (`open(..., "w")` + `json.dump`; nothing in src/ or execution/ uses
# copy2/copystat/utime), and every such rewrite moves mtime — so the §8
# "late-arriving cache file changes the answer" case is still caught. The tag
# makes the weaker guarantee explicit, and bumping it forces every previously-
# recorded pipeline_key to differ if the scheme changes again — without that, an
# attempt recorded under an old scheme could suppress a new one through
# deduplicate_completed.
_SOURCE_FINGERPRINT_SCHEME = "stat-v1"

FrozenSet = tuple[str, list[tuple[str, str, bool]]]
ScopeSlices = dict[tuple[str, str], list[str]]


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _file_stat_signature(path: Path) -> list[int] | None:
    """[st_mtime_ns, st_size], or None when the file is absent or unreadable.

    Absent and unreadable deliberately collapse to the same None: neither
    contributes usable input to the metrics, and both flip to a real signature
    the moment the file lands.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_size]


def _source_files_fingerprint(repo_root: Path, tickers: set[str]) -> tuple[str, int]:
    """Identify every member cache file by (mtime_ns, size) — see
    `_SOURCE_FINGERPRINT_SCHEME` for why this is stat-based, not content-based."""
    file_count = len(tickers) * len(_MEMBER_SOURCE_SUFFIXES)
    if file_count > _MAX_FINGERPRINT_FILES:
        raise ValueError(
            "comparable-set source fingerprint exceeds bounded file budget "
            f"({file_count} files from {len(tickers)} tickers > {_MAX_FINGERPRINT_FILES})"
        )
    rows: list[dict[str, object]] = []
    for ticker in sorted(tickers):
        for suffix in _MEMBER_SOURCE_SUFFIXES:
            path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_{suffix}.json"
            rows.append(
                {
                    "stat": _file_stat_signature(path),
                    "suffix": suffix,
                    "ticker": ticker.upper(),
                }
            )
    payload = {"files": rows, "scheme": _SOURCE_FINGERPRINT_SCHEME}
    return _canonical_sha256(payload), file_count


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    list_types = (
        TRACKED_SUBJECT_LIST_TYPES
        if getattr(args, "all_tracked", False)
        else frozenset({ListType.PORTFOLIO})
    )
    companies = tracked_companies_for_user(conn, only_classified=False, list_types=list_types)
    return sorted({c.ticker for c in companies})


def _run_scope_slices(
    conn: sqlite3.Connection,
    repo_root: Path,
    as_of: date,
    slices: ScopeSlices,
) -> tuple[list[dict[str, object]], int]:
    """Compute + upsert the §6 pool-wide industry/sector slice rows.

    Per-slice degrade (the standard per-item pattern): one bad slice is
    reported and skipped, the rest still write. Returns (per-slice results,
    failed count).
    """
    results: list[dict[str, object]] = []
    failed = 0
    for (scope_type, scope_key), member_tickers in slices.items():
        members = [
            ComparableSetMember(t, f"{scope_type}_slice", context_only=False)
            for t in member_tickers
        ]
        mclass = scope_metric_class(scope_type, scope_key)
        try:
            rows: list[MetricRow] = compute_comparable_set_metrics(
                repo_root, members, mclass, as_of
            )
            n_written = upsert_metric_rows(conn, scope_type, scope_key, as_of, METHOD_VERSION, rows)
            results.append(
                {
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "metric_class": mclass,
                    "n_members": len(member_tickers),
                    "rows_written": n_written,
                }
            )
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
            failed += 1
            results.append(
                {
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "error": f"{type(e).__name__}: {e}"[:300],
                }
            )
            sys.stderr.write(f"FAILED slice {scope_type}:{scope_key}: {type(e).__name__}: {e}\n")
    return results, failed


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
        "WHERE comparable_set_id = ? AND valid_to IS NULL "
        "ORDER BY member_ticker, membership_reason, context_only",
        (set_id,),
    )
    members = [
        (str(r["member_ticker"]), str(r["membership_reason"]), bool(r["context_only"]))
        for r in cur.fetchall()
    ]
    return metric_class, members


def _metric_input_fingerprint(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    repo_root: Path,
    include_pool_scopes: bool,
) -> tuple[dict[str, FrozenSet | None], ScopeSlices, str, str, int]:
    """Select and hash bounded membership rows plus every member cache file."""
    frozen_by_ticker = {ticker: _load_frozen_set(conn, ticker) for ticker in tickers}
    membership_count = sum(
        len(frozen[1]) for frozen in frozen_by_ticker.values() if frozen is not None
    )
    if membership_count > _MAX_FINGERPRINT_MEMBERSHIPS:
        raise ValueError(
            "comparable-set fingerprint exceeds bounded membership budget "
            f"({membership_count} > {_MAX_FINGERPRINT_MEMBERSHIPS})"
        )

    slices: ScopeSlices = {}
    if include_pool_scopes:
        slices = pool_scope_slices(load_pool(conn, repo_root))
        membership_count += sum(len(members) for members in slices.values())
        if membership_count > _MAX_FINGERPRINT_MEMBERSHIPS:
            raise ValueError(
                "pool-scope fingerprint exceeds bounded membership budget "
                f"({membership_count} across {len(slices)} slices > {_MAX_FINGERPRINT_MEMBERSHIPS})"
            )

    source_tickers: set[str] = set()
    frozen_payload: list[dict[str, object]] = []
    for ticker in sorted(frozen_by_ticker):
        frozen = frozen_by_ticker[ticker]
        if frozen is None:
            frozen_payload.append(
                {
                    "comparable_set_id": comparable_set_id(ticker, METHOD_VERSION),
                    "members": None,
                    "metric_class": None,
                    "ticker": ticker,
                }
            )
            continue
        metric_class, members = frozen
        frozen_payload.append(
            {
                "comparable_set_id": comparable_set_id(ticker, METHOD_VERSION),
                "members": [
                    {
                        "context_only": context_only,
                        "member_ticker": member_ticker,
                        "membership_reason": reason,
                    }
                    for member_ticker, reason, context_only in members
                ],
                "metric_class": metric_class,
                "ticker": ticker,
            }
        )
        if metric_class != "reit":
            source_tickers.update(
                member_ticker for member_ticker, _, context_only in members if not context_only
            )

    slice_payload: list[dict[str, object]] = []
    for (scope_type, scope_key), members in sorted(slices.items()):
        metric_class = scope_metric_class(scope_type, scope_key)
        slice_payload.append(
            {
                "members": members,
                "metric_class": metric_class,
                "scope_key": scope_key,
                "scope_type": scope_type,
            }
        )
        if metric_class != "reit":
            source_tickers.update(members)

    selection_fingerprint = _canonical_sha256(
        {
            "frozen_sets": frozen_payload,
            "pool_slices": slice_payload,
        }
    )
    source_fingerprint, file_count = _source_files_fingerprint(repo_root, source_tickers)
    return (
        frozen_by_ticker,
        slices,
        selection_fingerprint,
        source_fingerprint,
        file_count,
    )


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
        help="Widen subjects to portfolio+watchlist+evaluation (Phase 2 scope; "
        "ignored when --ticker is given)",
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

        (
            frozen_by_ticker,
            scope_slices,
            selection_fingerprint,
            source_fingerprint,
            source_file_count,
        ) = _metric_input_fingerprint(
            conn,
            tickers=tickers,
            repo_root=repo_root,
            include_pool_scopes=args.ticker is None,
        )
        try:
            run_id = start_run(
                conn,
                directive="track_comp_metrics",
                ticker_scope=tickers,
                invocation_inputs={
                    "as_of": as_of.isoformat(),
                    "include_pool_scopes": args.ticker is None,
                    "method_version": METHOD_VERSION,
                    "repo_root": str(repo_root.resolve()),
                    "selection_fingerprint": selection_fingerprint,
                    "source_file_count": source_file_count,
                    "source_files_fingerprint": source_fingerprint,
                },
                deduplicate_completed=True,
            )
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0
        results: list[dict[str, object]] = []
        skipped = 0
        failed = 0
        for ticker in tickers:
            frozen = frozen_by_ticker[ticker]
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

        # Phase 2 (§6): pool-wide industry/sector slice rows — subject-
        # independent, so computed once per run (skipped for a --ticker run,
        # which targets one subject's set only).
        scope_results: list[dict[str, object]] = []
        scope_failed = 0
        if not args.ticker:
            scope_results, scope_failed = _run_scope_slices(
                conn,
                repo_root,
                as_of,
                scope_slices,
            )
            record_stage(
                conn,
                run_id,
                POOL_SCOPES_STAGE_TICKER,
                StageName.COMPARABLE_SET_RESOLVE,
                StageStatus.OK if scope_failed == 0 else StageStatus.FAILED,
                error_msg=f"{scope_failed} slices failed" if scope_failed else None,
            )
            failed += scope_failed

        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        end_run(
            conn,
            run_id,
            terminal,
            error_summary=f"{failed} tickers/slices failed" if failed else None,
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
                    "scope_slices": scope_results,
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
