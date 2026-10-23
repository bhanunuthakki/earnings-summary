"""Run the deterministic thesis-break evaluator across one or more tracked holdings.

Reads `break_rules` from each `micro_thesis/holdings/<TICKER>.json`, joins them
against `kpi_facts`, and writes the rolled-up Red/Yellow/Green verdict back to
`thesis_state.breach_status`. Wraps the run in start_run / record_stage /
end_run so every invocation produces an audit trail.

Usage:
    python execution/run_thesis_evaluator.py --ticker MELI
    python execution/run_thesis_evaluator.py --all
    python execution/run_thesis_evaluator.py --all --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

try:
    from _lib import PROJECT_ROOT
except ImportError:  # ``python -m execution.run_thesis_evaluator``
    from execution._lib import PROJECT_ROOT

from compute.thesis_episode_attention import (
    AttentionError,
    deliver_due_episode_alerts,
)
from compute.thesis_evaluator import (
    ThesisVerdict,
    evaluate_ticker_thesis,
    persist_verdict,
)
from db_paths import configured_db_path
from models.runs import StageName, StageStatus
from pipeline.db_writer_gate import hold_writer_gate
from pipeline.invocation_fingerprint import files_fingerprint, payload_sha256
from pipeline.run_accounting import (
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    record_stage,
    start_run,
    suppression_payload,
)
from provenance.financial_fact_resolution import canonical_fact_relation
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from thesis_reunderwrite_gate import ReUnderwriteBlockedError

_HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"
_MATERIAL_TABLE_QUERIES = {
    "kpi_definitions": (
        "PRAGMA table_info(kpi_definitions)",
        "SELECT * FROM kpi_definitions WHERE UPPER(ticker) = ?",
    ),
    "fact_overrides": (
        "PRAGMA table_info(fact_overrides)",
        "SELECT * FROM fact_overrides WHERE UPPER(ticker) = ?",
    ),
    "decisions": (
        "PRAGMA table_info(decisions)",
        "SELECT * FROM decisions WHERE UPPER(ticker) = ?",
    ),
}


def _json_cell(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _table_snapshot_sha256(
    conn: sqlite3.Connection,
    *,
    table: str,
    tickers: list[str],
) -> str:
    if table in {"financial_facts", "kpi_facts"}:
        fact_table = "financial_facts" if table == "financial_facts" else "kpi_facts"
        relation = canonical_fact_relation(conn, fact_table).sql
        rows: list[list[JsonValue]] = []
        columns: list[str] = []
        for ticker in tickers:
            cursor = conn.execute(
                f"SELECT * FROM {relation} WHERE UPPER(ticker) = ?",  # nosec B608 -- trusted canonical relation; values remain bound
                (ticker,),
            )
            if not columns and cursor.description is not None:
                columns = [str(description[0]) for description in cursor.description]
            rows.extend([_json_cell(value) for value in row] for row in cursor.fetchall())
        rows.sort(key=lambda row: json.dumps(row, separators=(",", ":"), sort_keys=True))
        return payload_sha256({"table": table, "columns": columns, "rows": rows})
    try:
        schema_query, row_query = _MATERIAL_TABLE_QUERIES[table]
    except KeyError as exc:
        raise ValueError(f"unsupported material input table: {table}") from exc
    columns = [str(row[1]) for row in conn.execute(schema_query).fetchall()]
    if not columns:
        return payload_sha256({"table": table, "exists": False})
    if "ticker" not in columns:
        raise ValueError(f"material input table {table} has no ticker column")
    rows = [
        [_json_cell(value) for value in row]
        for ticker in tickers
        for row in conn.execute(row_query, (ticker,)).fetchall()
    ]
    rows.sort(key=lambda row: json.dumps(row, separators=(",", ":"), sort_keys=True))
    return payload_sha256({"table": table, "columns": columns, "rows": rows})


def _invocation_inputs(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    tickers: list[str],
) -> dict[str, JsonValue]:
    holdings_dir = args.holdings_dir.resolve()
    return {
        "holdings": files_fingerprint(
            [holdings_dir / f"{ticker}.json" for ticker in tickers],
            root=holdings_dir,
        ),
        "db_snapshots": {
            table: _table_snapshot_sha256(conn, table=table, tickers=tickers)
            for table in (*_MATERIAL_TABLE_QUERIES, "financial_facts", "kpi_facts")
        },
        "dry_run": bool(args.dry_run),
        "override": bool(args.override),
    }


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    """Return the list of tickers to evaluate. --ticker overrides --all."""
    if args.ticker:
        return [args.ticker.upper()]
    cur = conn.execute("SELECT ticker FROM thesis_state ORDER BY ticker")
    return [row["ticker"] for row in cur.fetchall()]


def _verdict_to_dict(verdict: ThesisVerdict) -> dict[str, object]:
    """Render a ThesisVerdict as a JSON-serializable dict."""
    return {
        "ticker": verdict.ticker,
        "thesis": verdict.thesis,
        "overall_status": verdict.overall_status.value,
        "evaluated_at": verdict.evaluated_at.isoformat(),
        "rule_evaluations": [
            {
                "rule_id": e.rule.rule_id,
                "kpi_name": e.rule.kpi_name,
                "comparator": e.rule.comparator.value,
                "threshold": str(e.rule.threshold),
                "consecutive_periods": e.rule.consecutive_periods,
                "status": e.status.value,
                "detail": e.detail,
                "narrative": e.rule.narrative,
                "observations": [
                    {
                        "period_end": obs.period_end.date().isoformat(),
                        "value": str(obs.value),
                    }
                    for obs in e.observations
                ],
            }
            for e in verdict.rule_evaluations
        ],
    }


@contextmanager
def _writer_window(db_path: Path, *, owner: str) -> Generator[sqlite3.Connection]:
    """One gated database write window for this evaluator run.

    The daily brief worker overlaps this subprocess with sibling chains on a
    bounded pool, so the evaluator must not hold a writer connection open for
    its whole lifetime (the pre-pool guarantee was one writer PROCESS at a
    time; the pool broke it). Reads and evaluation compute run on read-only
    connections and overlap freely; every mutation happens inside this window,
    which takes the same cross-process per-database writer gate the pool
    driver and the other chain steps honor — so at most one writer window is
    open on the database file at any instant, across processes.
    """
    with hold_writer_gate(db_path, owner=owner):
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            yield conn
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Evaluate a single ticker")
    group.add_argument("--all", action="store_true", help="Evaluate every ticker in thesis_state")
    parser.add_argument("--dry-run", action="store_true", help="Print verdicts without persisting")
    parser.add_argument(
        "--db",
        default=str(configured_db_path(PROJECT_ROOT)),
        help=(
            "Portfolio database. The daily brief worker passes the resolved "
            "database explicitly; the default is the configured authority "
            "(EARNINGS_SUMMARY_DB_PATH, else the repo-root default for the "
            "production host)."
        ),
    )
    parser.add_argument("--holdings-dir", default=str(_HOLDINGS_DIR), type=Path)
    parser.add_argument(
        "--override",
        action="store_true",
        help=(
            "Bypass the scored-miss re-underwrite gate (monthly_red_team.md Phase 3): "
            "normally, re-persisting a MATERIALLY changed thesis for a ticker currently "
            "warn/breach is blocked until `execution/log_scored_miss.py` has recorded a "
            "calibration entry for the belief that broke. Every use of --override is "
            "logged loudly (event=thesis_reunderwrite_gate_overridden) — it is never a "
            "silent bypass. Prefer logging the scored miss first."
        ),
    )
    args = parser.parse_args()
    db_path = Path(args.db)

    read_conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        tickers = _resolve_tickers(read_conn, args)
        if not tickers:
            print(json.dumps({"warning": "no tickers in thesis_state"}, indent=2))
            return 0

        with _writer_window(db_path, owner="run_thesis_evaluator:run") as run_conn:
            run_id = start_run(
                run_conn,
                directive="run_thesis_evaluator",
                ticker_scope=tickers,
                invocation_inputs=_invocation_inputs(read_conn, args, tickers),
            )
        verdicts: list[ThesisVerdict] = []
        skipped: list[dict[str, str]] = []
        failed = 0

        for ticker in tickers:
            try:
                verdict = evaluate_ticker_thesis(
                    read_conn, ticker=ticker, holdings_dir=args.holdings_dir
                )
            except FileNotFoundError as e:
                skipped.append({"ticker": ticker, "reason": str(e)})
                with _writer_window(db_path, owner=f"run_thesis_evaluator:{ticker}") as conn:
                    record_stage(
                        conn,
                        run_id,
                        ticker,
                        StageName.SYNTHESIZE,
                        StageStatus.SKIPPED,
                        error_msg=f"{type(e).__name__}: {e}"[:500],
                    )
                continue
            except (ValueError, KeyError) as e:
                failed += 1
                with _writer_window(db_path, owner=f"run_thesis_evaluator:{ticker}") as conn:
                    record_stage(
                        conn,
                        run_id,
                        ticker,
                        StageName.SYNTHESIZE,
                        StageStatus.FAILED,
                        error_msg=f"{type(e).__name__}: {e}"[:500],
                    )
                sys.stderr.write(f"FAILED {ticker}: {type(e).__name__}: {e}\n")
                continue

            verdicts.append(verdict)
            if not args.dry_run:
                try:
                    # One window per ticker's persistence: persist_verdict and
                    # the OK stage receipt share the same write batch. Pass
                    # holdings_dir so the thesis_state content mirror
                    # (raw_json/thesis) is re-synced from the file this verdict
                    # was built from — the mirror can't silently drift behind
                    # the file.
                    with _writer_window(db_path, owner=f"run_thesis_evaluator:{ticker}") as conn:
                        persist_verdict(
                            conn,
                            verdict,
                            run_id=run_id,
                            holdings_dir=args.holdings_dir,
                            override=args.override,
                        )
                        record_stage(conn, run_id, ticker, StageName.SYNTHESIZE, StageStatus.OK)
                except ReUnderwriteBlockedError as e:
                    failed += 1
                    with _writer_window(db_path, owner=f"run_thesis_evaluator:{ticker}") as conn:
                        record_stage(
                            conn,
                            run_id,
                            ticker,
                            StageName.SYNTHESIZE,
                            StageStatus.FAILED,
                            error_msg=str(e)[:500],
                        )
                    sys.stderr.write(f"BLOCKED {ticker}: {e}\n")
                    continue
            else:
                with _writer_window(db_path, owner=f"run_thesis_evaluator:{ticker}") as conn:
                    record_stage(conn, run_id, ticker, StageName.SYNTHESIZE, StageStatus.OK)

        due_review_alerts = 0
        if not args.dry_run:
            try:
                with _writer_window(db_path, owner="run_thesis_evaluator:episode-alerts") as conn:
                    due_review_alerts = deliver_due_episode_alerts(conn, now=datetime.now(UTC))
                    conn.commit()
            except (AttentionError, sqlite3.Error) as exc:
                failed += 1
                sys.stderr.write(f"FAILED due thesis review delivery: {exc}\n")
        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        with _writer_window(db_path, owner="run_thesis_evaluator:run") as conn:
            end_run(conn, run_id, terminal, error_summary=f"{failed} failed" if failed else None)

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "evaluated": len(verdicts),
                    "skipped": skipped,
                    "failed": failed,
                    "dry_run": args.dry_run,
                    "due_review_alerts": due_review_alerts,
                    "verdicts": [_verdict_to_dict(v) for v in verdicts],
                },
                indent=2,
            )
        )
        return 0 if failed == 0 else 1
    except PipelineRunSuppressedError as exc:
        print(json.dumps(suppression_payload(exc)))
        return 0
    finally:
        read_conn.close()


if __name__ == "__main__":
    sys.exit(main())
