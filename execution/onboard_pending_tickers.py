"""Find active-universe tickers missing data, analysis, or commitment-extraction
work and run the appropriate subset of the pipeline for each. Closes the gap
when tickers get added via raw SQL / direct DB writes / external API
bypass AND keeps the commitment ledger fresh as new transcripts arrive.

A ticker is "pending" when ALL of:
  - list_type IN (portfolio, watchlist, evaluation) — `db.ACTIVE_LIST_TYPES`

AND ANY of:
  - instrument_type IS NULL                  -> 'no_instrument_type'
    (track_company would have set this; missing means the ticker bypassed
    the auto-onboard hook)
  - 0 rows in financial_facts                -> 'no_financial_facts'
    (parse stage never ran)
  - 0 rows in dcf_runs                       -> 'no_dcf_run'
    (analysis stage never ran)
  - has transcripts but 0 management_commitments -> 'no_commitments'
    (commitments never extracted from existing transcripts)

Per-ticker work depends on pending_reason:
  - no_instrument_type / no_financial_facts / no_dcf_run:
      onboard_ticker -> run_thesis_evaluator -> refresh_dcf
      -> extract_commitments_from_transcript --auto
  - no_commitments:
      extract_commitments_from_transcript --auto only

Idempotent at every layer:
  - save_fmp_data --skip-existing on the FMP fetch
  - run_thesis_evaluator always recomputes from current facts
  - refresh_dcf seeds dcf/<TICKER>.xlsx if missing then re-runs the PV calc;
    skips with status='skipped' for tickers whose holdings JSON lacks WACC,
    leaving the dcf_runs row absent for those (no perpetual write churn).
  - extract_commitments --auto skips transcripts that already have at least
    one commitments row

Designed to be invoked hourly by Windows Task Scheduler. See:
  - directives/onboard_pending_tickers.md
  - cron/onboard_pending_tickers.task.xml
  - cron/run_onboard_pending.bat

Usage:
    python execution/onboard_pending_tickers.py
    python execution/onboard_pending_tickers.py --dry-run
    python execution/onboard_pending_tickers.py --max 10
    python execution/onboard_pending_tickers.py --skip-fmp
    python execution/onboard_pending_tickers.py --skip-commitments
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from pipeline.queries import open_db  # noqa: E402

_DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
_LOG_DIR = PROJECT_ROOT / ".tmp" / "cron_logs"

# When pending_reason is 'no_commitments', only the commitment-extract stage
# needs to run — the heavy onboard/eval/DCF stages are no-ops.
_COMMITMENT_ONLY_REASON = "no_commitments"

log = logging.getLogger("onboard_pending")


class StageOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class StageResult:
    stage: str
    outcome: StageOutcome
    rc: int
    detail: str


@dataclass(frozen=True)
class TickerResult:
    ticker: str
    pending_reason: str
    stages: tuple[StageResult, ...]
    elapsed_seconds: float


_PENDING_SQL = f"""
SELECT
  tc.ticker,
  CASE
    WHEN tc.instrument_type IS NULL THEN 'no_instrument_type'
    WHEN (SELECT COUNT(*) FROM financial_facts ff WHERE UPPER(ff.ticker) = UPPER(tc.ticker)) = 0
      THEN 'no_financial_facts'
    WHEN (SELECT COUNT(*) FROM dcf_runs d WHERE UPPER(d.ticker) = UPPER(tc.ticker)) = 0
      THEN 'no_dcf_run'
    WHEN EXISTS (SELECT 1 FROM transcripts t WHERE UPPER(t.ticker) = UPPER(tc.ticker))
         AND NOT EXISTS (
           SELECT 1 FROM management_commitments mc WHERE UPPER(mc.ticker) = UPPER(tc.ticker)
         )
      THEN 'no_commitments'
    ELSE 'ok'
  END AS pending_reason
FROM tracked_companies tc
WHERE tc.list_type IN {db.ACTIVE_LIST_TYPES_SQL}
  AND (
    tc.instrument_type IS NULL
    OR (SELECT COUNT(*) FROM financial_facts ff WHERE UPPER(ff.ticker) = UPPER(tc.ticker)) = 0
    OR (SELECT COUNT(*) FROM dcf_runs d WHERE UPPER(d.ticker) = UPPER(tc.ticker)) = 0
    OR (
      EXISTS (SELECT 1 FROM transcripts t WHERE UPPER(t.ticker) = UPPER(tc.ticker))
      AND NOT EXISTS (
        SELECT 1 FROM management_commitments mc WHERE UPPER(mc.ticker) = UPPER(tc.ticker)
      )
    )
  )
ORDER BY tc.added_at, tc.ticker
"""


def find_pending_tickers(db_path: Path) -> list[tuple[str, str]]:
    """Return [(ticker, pending_reason), ...] for every P+W ticker still missing onboard data."""
    conn = open_db(db_path)
    try:
        cur = conn.execute(_PENDING_SQL)
        return [(row["ticker"], row["pending_reason"]) for row in cur.fetchall()]
    finally:
        conn.close()


def _run_subprocess(cmd: list[str], stage: str, log_path: Path) -> StageResult:
    """Run a subprocess, append stdout/stderr to log_path, return a StageResult."""
    with open(log_path, "ab") as fh:
        fh.write(f"\n[{stage}] cmd: {' '.join(cmd)}\n".encode("utf-8"))
        fh.flush()
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    if proc.returncode == 0:
        return StageResult(stage=stage, outcome=StageOutcome.OK, rc=0, detail="")
    return StageResult(
        stage=stage,
        outcome=StageOutcome.FAILED,
        rc=proc.returncode,
        detail=f"exit code {proc.returncode}",
    )


def _skipped(stage: str, detail: str) -> StageResult:
    return StageResult(stage=stage, outcome=StageOutcome.SKIPPED, rc=0, detail=detail)


def onboard_one(
    ticker: str,
    pending_reason: str,
    *,
    skip_fmp: bool,
    skip_commitments: bool,
    log_path: Path,
) -> TickerResult:
    """Run the appropriate stage chain for a single ticker. Returns a structured result.

    Stage subset depends on pending_reason:
      - no_commitments        -> commitment_extract only (heavy stages skipped)
      - any other reason      -> full chain (onboard + eval + DCF + commitment_extract)

    All stages best-effort: failures are surfaced in the result, not raised.
    """
    started = datetime.now(timezone.utc)
    stages: list[StageResult] = []
    is_commitment_only = pending_reason == _COMMITMENT_ONLY_REASON

    if is_commitment_only:
        stages.append(_skipped("onboard_ticker", "ticker already onboarded"))
        stages.append(_skipped("run_thesis_evaluator", "ticker already evaluated"))
        stages.append(_skipped("refresh_dcf", "ticker already has DCF"))
    else:
        onboard_cmd = [sys.executable, "execution/onboard_ticker.py", "--ticker", ticker]
        if skip_fmp:
            onboard_cmd.append("--skip-fmp")
        stages.append(_run_subprocess(onboard_cmd, "onboard_ticker", log_path))

        # run_thesis_evaluator is best-effort — missing holdings JSON returns non-zero
        # but should not abort the rest of the chain.
        eval_cmd = [sys.executable, "execution/run_thesis_evaluator.py", "--ticker", ticker]
        stages.append(_run_subprocess(eval_cmd, "run_thesis_evaluator", log_path))

        # refresh_dcf replaces the old batch_dcf path: seeds dcf/<TICKER>.xlsx
        # if missing, refreshes its Historicals, then re-runs the PV calc.
        dcf_cmd = [sys.executable, "execution/refresh_dcf.py", "--ticker", ticker]
        stages.append(_run_subprocess(dcf_cmd, "refresh_dcf", log_path))

    if skip_commitments:
        stages.append(_skipped("extract_commitments", "--skip-commitments flag"))
    else:
        # Auto-extract any pending commitments from this ticker's transcripts.
        # Idempotent: transcripts already with a commitment row are skipped.
        # Best-effort: missing LLM auth / no transcripts is non-fatal.
        commit_cmd = [
            sys.executable,
            "execution/extract_commitments_from_transcript.py",
            "--auto",
            "--ticker",
            ticker,
        ]
        stages.append(_run_subprocess(commit_cmd, "extract_commitments", log_path))

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return TickerResult(
        ticker=ticker,
        pending_reason=pending_reason,
        stages=tuple(stages),
        elapsed_seconds=elapsed,
    )


def _result_to_dict(r: TickerResult) -> dict[str, object]:
    return {
        "ticker": r.ticker,
        "pending_reason": r.pending_reason,
        "elapsed_seconds": round(r.elapsed_seconds, 1),
        "stages": [
            {"stage": s.stage, "outcome": s.outcome.value, "rc": s.rc, "detail": s.detail}
            for s in r.stages
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(_DB_PATH), help="Path to portfolio.db")
    ap.add_argument("--dry-run", action="store_true", help="List pending tickers and exit")
    ap.add_argument("--max", type=int, default=0, help="Limit to first N tickers (0 = no limit)")
    ap.add_argument("--skip-fmp", action="store_true", help="Skip FMP fetch (parse-only re-run)")
    ap.add_argument(
        "--skip-commitments",
        action="store_true",
        help="Skip the LLM commitment-extraction stage (faster runs; useful when LLM auth is unavailable)",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="[onboard_pending] %(message)s")
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = _LOG_DIR / f"onboard_pending_{stamp}.log"

    pending = find_pending_tickers(Path(args.db))
    if not pending:
        report = {"run_id": stamp, "pending_count": 0, "results": [], "log": str(log_path)}
        print(json.dumps(report, indent=2))
        return 0

    if args.max > 0:
        pending = pending[: args.max]

    if args.dry_run:
        print(json.dumps(
            {
                "run_id": stamp,
                "pending_count": len(pending),
                "tickers": [{"ticker": t, "reason": r} for t, r in pending],
            },
            indent=2,
        ))
        return 0

    log.info("starting run %s — %d pending tickers — log: %s", stamp, len(pending), log_path)
    results: list[TickerResult] = []
    for ticker, reason in pending:
        log.info("processing %s (%s)", ticker, reason)
        result = onboard_one(
            ticker,
            reason,
            skip_fmp=args.skip_fmp,
            skip_commitments=args.skip_commitments,
            log_path=log_path,
        )
        results.append(result)
        outcomes = " ".join(
            f"{s.stage.split('_')[0]}={s.outcome.value}" for s in result.stages
        )
        log.info("  %s done in %.1fs — %s", ticker, result.elapsed_seconds, outcomes)

    report = {
        "run_id": stamp,
        "pending_count": len(pending),
        "log": str(log_path),
        "results": [_result_to_dict(r) for r in results],
    }
    print(json.dumps(report, indent=2))

    # Exit code: non-zero if every onboard subprocess failed (signals real problem).
    # Pure no-commitments runs skip onboard, so they never count toward this signal.
    full_chain_results = [
        r for r in results if r.pending_reason != _COMMITMENT_ONLY_REASON
    ]
    onboard_failures = sum(
        1 for r in full_chain_results if r.stages[0].outcome is StageOutcome.FAILED
    )
    return (
        1
        if full_chain_results
        and onboard_failures == len(full_chain_results)
        else 0
    )


if __name__ == "__main__":
    sys.exit(main())
