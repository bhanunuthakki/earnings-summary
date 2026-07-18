"""Compute bottoms-up derived metrics (Phase 1) from financial_facts and
persist into kpi_facts + metric_computation_attempts.

Modeled directly on execution/derive_kpis_from_fmp.py's shape. Idempotent:
a re-run over unchanged data skips every (ticker, period, formula) whose
input_fingerprint hasn't changed (--force bypasses the skip). Structured
JSON-line events go to stderr; the only stdout is the final summary.

Usage:
    python execution/compute_derived_metrics.py --ticker MELI
    python execution/compute_derived_metrics.py --all
    python execution/compute_derived_metrics.py --all --scope portfolio
    python execution/compute_derived_metrics.py --all --force
    python execution/compute_derived_metrics.py --parity --ticker MELI
    python execution/compute_derived_metrics.py --parity --all --db ../earnings-summary/data/portfolio.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.metrics_engine.io import compute_for_ticker  # noqa: E402
from compute.metrics_engine.parity import ParityOutcome, compare_ticker  # noqa: E402
from models.companies import ListType  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.queries import BRIEFED_LIST_TYPES, open_db, tracked_companies_for_user  # noqa: E402
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402

_SCOPE_TO_LIST_TYPE: dict[str, ListType] = {
    "portfolio": ListType.PORTFOLIO,
    "watchlist": ListType.WATCHLIST,
    "evaluation": ListType.EVALUATION,
}


def _log_event(event: str, **fields: object) -> None:
    """One JSON line per event, to stderr (data stays on stdout)."""
    sys.stderr.write(json.dumps({"event": event, **fields}, default=str) + "\n")


def _resolve_scope(scope_arg: str) -> frozenset[ListType]:
    if not scope_arg:
        return BRIEFED_LIST_TYPES
    names = [s.strip().lower() for s in scope_arg.split(",") if s.strip()]
    try:
        return frozenset(_SCOPE_TO_LIST_TYPE[n] for n in names)
    except KeyError as exc:
        raise SystemExit(
            f"--scope: unknown list_type {exc}; choose from {sorted(_SCOPE_TO_LIST_TYPE)}"
        ) from exc


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    companies = tracked_companies_for_user(conn, list_types=_resolve_scope(args.scope))
    return [c.ticker for c in companies]


def _run_compute(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    tickers = _resolve_tickers(conn, args)
    if not tickers:
        print(json.dumps({"warning": "no tickers in scope"}, indent=2))
        return 0

    run_id = start_run(conn, directive="compute_derived_metrics", ticker_scope=tickers)
    per_ticker: list[dict[str, object]] = []
    failed = 0

    for ticker in tickers:
        try:
            summary = compute_for_ticker(conn, ticker, force=args.force)
        except (ValueError, KeyError) as e:
            failed += 1
            record_stage(
                conn,
                run_id,
                ticker,
                StageName.COMPUTE,
                StageStatus.FAILED,
                error_msg=f"{type(e).__name__}: {e}"[:500],
            )
            _log_event("compute_derived_metrics_failed", ticker=ticker, error=str(e))
            continue

        status = StageStatus.OK if summary.attempts_written > 0 else StageStatus.SKIPPED
        record_stage(conn, run_id, ticker, StageName.COMPUTE, status)
        _log_event(
            "compute_derived_metrics_ticker_done",
            ticker=ticker,
            business_model=summary.business_model.value,
            accounting_standard=summary.accounting_standard.value,
            quarters_seen=summary.quarters_seen,
            attempts_written=summary.attempts_written,
            computed_ok=summary.computed_ok,
            not_computable=summary.not_computable,
            skipped_unchanged=summary.skipped_unchanged,
        )
        per_ticker.append(
            {
                "ticker": ticker,
                "business_model": summary.business_model.value,
                "accounting_standard": summary.accounting_standard.value,
                "quarters_seen": summary.quarters_seen,
                "attempts_written": summary.attempts_written,
                "computed_ok": summary.computed_ok,
                "not_computable": summary.not_computable,
                "skipped_unchanged": summary.skipped_unchanged,
            }
        )

    terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
    end_run(conn, run_id, terminal, error_summary=f"{failed} failed" if failed else None)

    print(
        json.dumps(
            {
                "run_id": run_id,
                "tickers_processed": len(per_ticker),
                "failed": failed,
                "per_ticker": per_ticker,
            },
            indent=2,
        )
    )
    return 0 if failed == 0 else 1


def _run_parity(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    tickers = _resolve_tickers(conn, args)
    if not tickers:
        print(json.dumps({"warning": "no tickers in scope"}, indent=2))
        return 0

    fmp_cache_dir = (
        Path(args.fmp_cache_dir)
        if args.fmp_cache_dir
        else Path(args.db).resolve().parent / "historical" / "fmp"
    )

    all_results: list[dict[str, object]] = []
    tally: dict[str, int] = {
        ParityOutcome.MATCH: 0,
        ParityOutcome.KNOWN_MISMATCH: 0,
        ParityOutcome.FAIL: 0,
        ParityOutcome.NO_DATA: 0,
    }
    unexpected_mismatches: list[dict[str, object]] = []

    for ticker in tickers:
        for result in compare_ticker(conn, fmp_cache_dir, ticker):
            tally[result.outcome] = tally.get(result.outcome, 0) + 1
            row: dict[str, object] = {
                "ticker": result.ticker,
                "formula_key": result.formula_key,
                "computed": str(result.computed) if result.computed is not None else None,
                "reference": str(result.reference) if result.reference is not None else None,
                "outcome": result.outcome,
                "detail": result.detail,
            }
            all_results.append(row)
            if result.outcome == ParityOutcome.FAIL:
                unexpected_mismatches.append(row)
            _log_event("parity_result", **row)

    tmp_dir = PROJECT_ROOT / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = tmp_dir / f"metrics_engine_parity_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "tickers_checked": len(tickers),
                "comparisons": len(all_results),
                "tally": tally,
                "unexpected_mismatches": len(unexpected_mismatches),
                "detail_file": str(out_path),
            },
            indent=2,
        )
    )
    return 0 if not unexpected_mismatches else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single ticker to compute/compare for")
    group.add_argument("--all", action="store_true", help="Every tracked company in --scope")
    parser.add_argument(
        "--scope",
        default="portfolio,evaluation",
        help="Comma-separated list_types for --all (default: portfolio,evaluation, "
        "matching pipeline.queries.BRIEFED_LIST_TYPES)",
    )
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    parser.add_argument(
        "--force", action="store_true", help="Bypass the input-fingerprint recompute skip"
    )
    parser.add_argument(
        "--parity",
        action="store_true",
        help="Run the FMP parity sweep instead of computing (read-only; writes .tmp/ detail)",
    )
    parser.add_argument(
        "--fmp-cache-dir",
        default=None,
        help="Override the cached FMP JSON directory for --parity (default: "
        "<db's parent>/historical/fmp)",
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        if args.parity:
            return _run_parity(conn, args)
        return _run_compute(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
