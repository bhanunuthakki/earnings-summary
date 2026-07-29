"""Extract quarterly segment data from as-filed 10-Q JSONs into the
segment_periods + segment_dimensions junction (docs/design/
segment_quarterly_framework.md, Phase 1 — 10-K-regime domestic filers only).

Usage:
    python execution/extract_segment_quarterly.py --ticker MELI
    python execution/extract_segment_quarterly.py --ticker MELI --year 2026 --quarter Q2
    python execution/extract_segment_quarterly.py --all
    python execution/extract_segment_quarterly.py --all --refresh

Routing: only tickers whose ``tracked_companies.filing_regime == '10-K'``
(``source_routing.segment_pipeline_for_regime`` returns ``tenq_10k_regime``)
are in scope for this Phase-1 script — 20-F/40-F tickers are Phase-3-spike-
gated and logged as skipped, never silently dropped from the roster.

Without an explicit ``--year``/``--quarter``, discovers every
``{TICKER}_form_10q_{Y}_{Q}.json`` already on disk under
``data/historical/fmp/`` for the resolved ticker(s) and processes each.

Audit: each invocation writes an ``ingestion_runs`` row + per-(ticker, year,
quarter) ``stage_transitions`` rows (stage=COMPUTE), matching
``extract_segment_crosstabs.py``'s wiring.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from compute.segment_quarterly_10q import extract_for_ticker  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.invocation_fingerprint import files_fingerprint, payload_sha256  # noqa: E402
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
from table_extractors.period_axis import NominalQuarter  # noqa: E402

_TENQ_FILENAME_RX = re.compile(r"^([A-Z][A-Z0-9.\-]*)_form_10q_(\d{4})_(Q[1-3])\.json$")


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
    jobs = _resolve_jobs(repo_root, conn, tickers, args)
    if not jobs:
        print("[]")
        conn.close()
        return 0

    try:
        run_id = start_run(
            conn,
            directive="extract_segment_quarterly",
            ticker_scope=sorted({j[0] for j in jobs}),
            invocation_inputs=_invocation_inputs(repo_root, conn, jobs),
            force=bool(args.refresh),
            deduplicate_completed=True,
        )
    except PipelineRunSuppressedError as exc:
        print(json.dumps(suppression_payload(exc)))
        conn.close()
        return 0
    summary: list[dict[str, object]] = []
    final_status = StageStatus.OK
    error_summary: str | None = None

    try:
        for ticker, year, quarter in jobs:
            record_stage(conn, run_id, ticker, StageName.COMPUTE, StageStatus.IN_PROGRESS)
            try:
                result = extract_for_ticker(
                    ticker,
                    year,
                    quarter,
                    repo_root,
                    conn,
                    db_path=db_path,
                    refresh=args.refresh,
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                record_stage(
                    conn, run_id, ticker, StageName.COMPUTE, StageStatus.FAILED, error_msg=err
                )
                summary.append({"ticker": ticker, "year": year, "quarter": quarter, "error": err})
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
                    "year": year,
                    "quarter": quarter,
                    "periods_inserted": result.periods_inserted,
                    "dimensions_inserted": result.dimensions_inserted,
                    "derived_inserted": result.derived_inserted,
                    "llm_fallback_calls": result.llm_fallback_calls,
                    "skipped": result.skipped_reason,
                    "skip_counts": result.skip_counts,
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
    g.add_argument("--all", action="store_true", help="All 10-K-regime tracked tickers")
    p.add_argument("--year", type=int, default=None, help="Specific fiscal year")
    p.add_argument("--quarter", choices=["Q1", "Q2", "Q3"], default=None, help="Specific quarter")
    p.add_argument("--refresh", action="store_true", help="Ignore cache and re-extract")
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


def _tenq_files_for_ticker(repo_root: Path, ticker: str) -> list[tuple[int, NominalQuarter]]:
    """[(year, quarter)] for every ``{ticker}_form_10q_{Y}_{Q}.json`` on disk,
    Y/Q ascending. The regex's ``Q[1-3]`` group is a validated external-data
    boundary (the filename on disk), so the cast to NominalQuarter is safe."""
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    if not fmp_dir.exists():
        return []
    out: list[tuple[int, NominalQuarter]] = []
    for path in fmp_dir.glob(f"{ticker}_form_10q_*_Q*.json"):
        m = _TENQ_FILENAME_RX.match(path.name)
        if m is None or m.group(1) != ticker:
            continue
        out.append((int(m.group(2)), cast("NominalQuarter", m.group(3))))
    return sorted(out)


def _resolve_jobs(
    repo_root: Path, conn: sqlite3.Connection, tickers: list[str], args: argparse.Namespace
) -> list[tuple[str, int, NominalQuarter]]:
    jobs: list[tuple[str, int, NominalQuarter]] = []
    for ticker in tickers:
        try:
            plan = plan_for_ticker(conn, ticker)
        except ValueError:
            continue
        if plan.segment_quarterly_pipeline != "tenq_10k_regime":
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "segment_quarterly_ticker_skipped",
                        "reason": "not_10k_regime",
                        "ticker": ticker,
                        "segment_quarterly_pipeline": plan.segment_quarterly_pipeline,
                    }
                )
                + "\n"
            )
            continue
        if args.year is not None and args.quarter is not None:
            # argparse's choices=["Q1","Q2","Q3"] is the validated boundary.
            jobs.append((ticker, args.year, cast("NominalQuarter", args.quarter)))
            continue
        for year, quarter in _tenq_files_for_ticker(repo_root, ticker):
            if args.year is not None and year != args.year:
                continue
            if args.quarter is not None and quarter != args.quarter:
                continue
            jobs.append((ticker, year, quarter))
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
    repo_root: Path,
    conn: sqlite3.Connection,
    jobs: list[tuple[str, int, NominalQuarter]],
) -> dict[str, JsonValue]:
    """Capture as-filed bytes plus all mutable DB inputs used by derivation."""
    tickers = sorted({job[0] for job in jobs})
    placeholders = ",".join("?" for _ in tickers)
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    source_paths = [
        fmp_dir / f"{ticker}_form_10q_{year}_{quarter}.json" for ticker, year, quarter in jobs
    ]
    for ticker in tickers:
        source_paths.extend(fmp_dir.glob(f"{ticker}_form_10k_*.json"))
    financial_facts = canonical_fact_relation(conn, "financial_facts").sql
    database_inputs = {
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
    }
    return {
        "jobs": [
            {"ticker": ticker, "year": year, "quarter": quarter} for ticker, year, quarter in jobs
        ],
        "source_files": files_fingerprint(source_paths, root=repo_root),
        "database_inputs": database_inputs,
    }


if __name__ == "__main__":
    raise SystemExit(main())
