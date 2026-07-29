"""Derive Q4 segment values (FY - Sigma Q1..Q3) into the segment_periods +
segment_dimensions junction (docs/design/segment_quarterly_framework.md §3,
Phase 2).

Usage:
    python execution/derive_q4_segments.py --ticker MELI --year 2025
    python execution/derive_q4_segments.py --ticker MELI
    python execution/derive_q4_segments.py --all
    python execution/derive_q4_segments.py --all --year 2025

Without an explicit ``--year``, discovers every fiscal year already carrying
an FY segment_periods row for the resolved ticker(s) and attempts a
derivation for each -- idempotent (a re-run against unchanged inputs is a
no-op; a changed input re-derives and chains via ``supersedes_id``, §3.2).

Routing: tickers on either quarterly segment route -- ``tenq_10k_regime``
(the 10-Q gate ``extract_segment_quarterly.py`` uses) or ``fpi_6k`` (FPIs
whose Q1-Q3 come from 6-K interim reports via
``extract_segment_quarterly_6k.py`` and whose FY comes from the annual
20-F; foreign filers publish no Q4 interim, so FY - (Q1+Q2+Q3) is the only
way their Q4 exists at all). Tickers with no quarterly segment route are
skipped with an honest reason.

Audit: each invocation writes an ``ingestion_runs`` row + per-(ticker, year)
``stage_transitions`` rows (stage=COMPUTE), matching
``extract_segment_quarterly.py``'s wiring.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from compute.segment_q4_derive import METHOD_VERSION, derive_for_ticker  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.invocation_fingerprint import payload_sha256  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    record_stage,
    start_run,
    suppression_payload,
)
from pipeline.source_routing import plan_for_ticker  # noqa: E402
from provenance.financial_fact_resolution import canonical_fact_relation  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        print(f"[error] no DB at {db_path}", file=sys.stderr)
        return 1

    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row

    tickers = _resolve_tickers(conn, args)
    jobs = _resolve_jobs(conn, tickers, args)
    if not jobs:
        print("[]")
        conn.close()
        return 0

    try:
        run_id = start_run(
            conn,
            directive="derive_q4_segments",
            ticker_scope=sorted({j[0] for j in jobs}),
            invocation_inputs=_invocation_inputs(conn, jobs),
        )
    except PipelineRunSuppressedError as exc:
        print(json.dumps(suppression_payload(exc)))
        conn.close()
        return 0
    summary: list[dict[str, object]] = []
    final_status = StageStatus.OK
    error_summary: str | None = None

    try:
        for ticker, year in jobs:
            record_stage(conn, run_id, ticker, StageName.COMPUTE, StageStatus.IN_PROGRESS)
            try:
                result = derive_for_ticker(ticker, year, repo_root, conn)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                record_stage(
                    conn, run_id, ticker, StageName.COMPUTE, StageStatus.FAILED, error_msg=err
                )
                summary.append({"ticker": ticker, "fiscal_year": year, "error": err})
                final_status = StageStatus.FAILED
                error_summary = err
                continue
            if result.skipped_reason is not None:
                record_stage(
                    conn,
                    run_id,
                    ticker,
                    StageName.COMPUTE,
                    StageStatus.SKIPPED,
                    error_msg=result.skipped_reason,
                )
            else:
                record_stage(conn, run_id, ticker, StageName.COMPUTE, StageStatus.OK)
            summary.append(
                {
                    "ticker": ticker,
                    "fiscal_year": year,
                    "derived_inserted": result.derived_inserted,
                    "superseded_count": result.superseded_count,
                    "not_computable_count": result.not_computable_count,
                    "tolerance_breach_count": result.tolerance_breach_count,
                    "skipped": result.skipped_reason,
                    "reason_counts": result.reason_counts,
                }
            )
    finally:
        end_run(conn, run_id, final_status, error_summary=error_summary)
        conn.close()

    print(json.dumps(summary, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker")
    g.add_argument(
        "--all", action="store_true", help="All tracked tickers with a quarterly segment route"
    )
    p.add_argument("--year", type=int, default=None, help="Specific fiscal year")
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args()


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    cur = conn.cursor()
    cur.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} ORDER BY ticker"
    )
    return [r[0] for r in cur.fetchall()]


def _fy_years_on_file(conn: sqlite3.Connection, ticker: str) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT CAST(strftime('%Y', period_end) AS INTEGER)
        FROM segment_periods
        WHERE ticker = ? AND fiscal_period_type = 'FY'
        ORDER BY 1
        """,
        (ticker,),
    ).fetchall()
    return [int(r[0]) for r in rows if r[0] is not None]


def _resolve_jobs(
    conn: sqlite3.Connection, tickers: list[str], args: argparse.Namespace
) -> list[tuple[str, int]]:
    jobs: list[tuple[str, int]] = []
    for ticker in tickers:
        try:
            plan = plan_for_ticker(conn, ticker)
        except ValueError:
            continue
        if plan.segment_quarterly_pipeline not in ("tenq_10k_regime", "fpi_6k"):
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "segment_q4_derive_ticker_skipped",
                        "reason": "no_quarterly_segment_route",
                        "ticker": ticker,
                        "segment_quarterly_pipeline": plan.segment_quarterly_pipeline,
                    }
                )
                + "\n"
            )
            continue
        if args.year is not None:
            jobs.append((ticker, args.year))
            continue
        for year in _fy_years_on_file(conn, ticker):
            jobs.append((ticker, year))
    return jobs


def _rows_sha256(rows: list[sqlite3.Row]) -> str:
    payload: list[JsonValue] = []
    for row in rows:
        payload.append(
            [
                value if value is None or isinstance(value, (str, int, float, bool)) else str(value)
                for value in row
            ]
        )
    return payload_sha256(payload)


def _invocation_inputs(
    conn: sqlite3.Connection, jobs: list[tuple[str, int]]
) -> dict[str, JsonValue]:
    """Fingerprint every DB row that can affect FY-minus-Q1-Q3 derivation."""
    tickers = sorted({ticker for ticker, _ in jobs})
    placeholders = ",".join("?" for _ in tickers)
    financial_facts = canonical_fact_relation(conn, "financial_facts").sql
    return {
        "jobs": [{"ticker": ticker, "fiscal_year": fiscal_year} for ticker, fiscal_year in jobs],
        "method_version": METHOD_VERSION,
        "database_inputs": {
            "tracked_companies": _rows_sha256(
                conn.execute(
                    f"SELECT * FROM tracked_companies WHERE ticker IN ({placeholders}) "
                    "ORDER BY ticker, user_id",
                    tickers,
                ).fetchall()
            ),
            "documents": _rows_sha256(
                conn.execute(
                    f"SELECT * FROM documents WHERE ticker IN ({placeholders}) ORDER BY id",
                    tickers,
                ).fetchall()
            ),
            "segment_facts": _rows_sha256(
                conn.execute(
                    "SELECT sp.*, sd.* FROM segment_periods sp "
                    "LEFT JOIN segment_dimensions sd ON sd.period_id = sp.id "
                    f"WHERE sp.ticker IN ({placeholders}) ORDER BY sp.id, sd.id",
                    tickers,
                ).fetchall()
            ),
            "financial_facts": _rows_sha256(
                conn.execute(
                    f"SELECT * FROM {financial_facts} "  # nosec B608 -- trusted canonical relation; values remain bound
                    f"WHERE ticker IN ({placeholders}) ORDER BY id",
                    tickers,
                ).fetchall()
            ),
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
