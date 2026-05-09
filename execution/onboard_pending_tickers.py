"""Find P+W tickers that bypassed `db.track_company`'s auto-onboard hook
and run `onboard_ticker.py` for each. Closes the gap when tickers get
added via raw SQL / direct DB writes / external API bypass.

A ticker is "pending onboard" if ALL of:
  - list_type IN ('portfolio', 'watchlist')

AND ANY of:
  - instrument_type IS NULL                  (track_company would have set it)
  - 0 rows in financial_facts for ticker     (parse stage never ran)
  - 0 rows in dcf_runs for ticker            (analysis stage never ran)

Per-ticker work:
  1. `execution/onboard_ticker.py --ticker T`     (subprocess; FMP fetch + parse)
  2. `execution/run_thesis_evaluator.py --ticker T` (subprocess; non-fatal if no holdings JSON)
  3. `execution/batch_dcf.py --ticker T`          (subprocess; non-fatal if facts insufficient)

Idempotent: `save_fmp_data --skip-existing` on the FMP fetch and
`run_thesis_evaluator` checks `breach_status` so re-runs are no-ops once a
ticker is fully onboarded.

Designed to be invoked hourly by Windows Task Scheduler. See:
  - directives/onboard_pending_tickers.md
  - cron/onboard_pending_tickers.task.xml
  - cron/run_onboard_pending.bat

Usage:
    python execution/onboard_pending_tickers.py
    python execution/onboard_pending_tickers.py --dry-run
    python execution/onboard_pending_tickers.py --max 10
    python execution/onboard_pending_tickers.py --skip-fmp
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

from pipeline.queries import open_db  # noqa: E402

_DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
_LOG_DIR = PROJECT_ROOT / ".tmp" / "cron_logs"

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


_PENDING_SQL = """
SELECT
  tc.ticker,
  CASE
    WHEN tc.instrument_type IS NULL THEN 'no_instrument_type'
    WHEN (SELECT COUNT(*) FROM financial_facts ff WHERE UPPER(ff.ticker) = UPPER(tc.ticker)) = 0
      THEN 'no_financial_facts'
    WHEN (SELECT COUNT(*) FROM dcf_runs d WHERE UPPER(d.ticker) = UPPER(tc.ticker)) = 0
      THEN 'no_dcf_run'
    ELSE 'ok'
  END AS pending_reason
FROM tracked_companies tc
WHERE tc.list_type IN ('portfolio', 'watchlist')
  AND (
    tc.instrument_type IS NULL
    OR (SELECT COUNT(*) FROM financial_facts ff WHERE UPPER(ff.ticker) = UPPER(tc.ticker)) = 0
    OR (SELECT COUNT(*) FROM dcf_runs d WHERE UPPER(d.ticker) = UPPER(tc.ticker)) = 0
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


def onboard_one(ticker: str, pending_reason: str, *, skip_fmp: bool, log_path: Path) -> TickerResult:
    """Run onboard + evaluator + DCF for a single ticker. Returns a structured result."""
    started = datetime.now(timezone.utc)
    stages: list[StageResult] = []

    onboard_cmd = [sys.executable, "execution/onboard_ticker.py", "--ticker", ticker]
    if skip_fmp:
        onboard_cmd.append("--skip-fmp")
    stages.append(_run_subprocess(onboard_cmd, "onboard_ticker", log_path))

    # run_thesis_evaluator is best-effort — missing holdings JSON returns non-zero
    # but should not abort the rest of the chain.
    eval_cmd = [sys.executable, "execution/run_thesis_evaluator.py", "--ticker", ticker]
    stages.append(_run_subprocess(eval_cmd, "run_thesis_evaluator", log_path))

    dcf_cmd = [sys.executable, "execution/batch_dcf.py", "--ticker", ticker]
    stages.append(_run_subprocess(dcf_cmd, "batch_dcf", log_path))

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
        log.info("onboarding %s (%s)", ticker, reason)
        result = onboard_one(ticker, reason, skip_fmp=args.skip_fmp, log_path=log_path)
        results.append(result)
        log.info(
            "  %s done in %.1fs — onboard=%s eval=%s dcf=%s",
            ticker,
            result.elapsed_seconds,
            result.stages[0].outcome.value,
            result.stages[1].outcome.value,
            result.stages[2].outcome.value,
        )

    report = {
        "run_id": stamp,
        "pending_count": len(pending),
        "log": str(log_path),
        "results": [_result_to_dict(r) for r in results],
    }
    print(json.dumps(report, indent=2))

    # Exit code: non-zero if every onboard subprocess failed (signals real problem)
    onboard_failures = sum(1 for r in results if r.stages[0].outcome is StageOutcome.FAILED)
    return 1 if onboard_failures and onboard_failures == len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
