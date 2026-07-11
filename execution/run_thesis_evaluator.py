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
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.thesis_evaluator import (  # noqa: E402
    ThesisVerdict,
    evaluate_ticker_thesis,
    persist_verdict,
)
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402
from thesis_reunderwrite_gate import ReUnderwriteBlockedError  # noqa: E402

_HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"


def _resolve_tickers(conn, args: argparse.Namespace) -> list[str]:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Evaluate a single ticker")
    group.add_argument("--all", action="store_true", help="Evaluate every ticker in thesis_state")
    parser.add_argument("--dry-run", action="store_true", help="Print verdicts without persisting")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
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

    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(conn, args)
        if not tickers:
            print(json.dumps({"warning": "no tickers in thesis_state"}, indent=2))
            return 0

        run_id = start_run(conn, directive="run_thesis_evaluator", ticker_scope=tickers)
        verdicts: list[ThesisVerdict] = []
        skipped: list[dict[str, str]] = []
        failed = 0

        for ticker in tickers:
            try:
                verdict = evaluate_ticker_thesis(
                    conn, ticker=ticker, holdings_dir=args.holdings_dir
                )
            except FileNotFoundError as e:
                skipped.append({"ticker": ticker, "reason": str(e)})
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
                    # Pass holdings_dir so the thesis_state content mirror
                    # (raw_json/thesis) is re-synced from the file this verdict was
                    # built from — the mirror can't silently drift behind the file.
                    persist_verdict(
                        conn,
                        verdict,
                        run_id=run_id,
                        holdings_dir=args.holdings_dir,
                        override=args.override,
                    )
                except ReUnderwriteBlockedError as e:
                    failed += 1
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
            record_stage(
                conn,
                run_id,
                ticker,
                StageName.SYNTHESIZE,
                StageStatus.OK,
            )

        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        end_run(conn, run_id, terminal, error_summary=f"{failed} failed" if failed else None)

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "evaluated": len(verdicts),
                    "skipped": skipped,
                    "failed": failed,
                    "dry_run": args.dry_run,
                    "verdicts": [_verdict_to_dict(v) for v in verdicts],
                },
                indent=2,
            )
        )
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
