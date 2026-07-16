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

Recently-IPO'd backoff (apply_ipo_backoff):
  A ticker flagged `recently_ipod: true` in micro_thesis/holdings/<T>.json has
  almost no FMP coverage until its first 10-Q is ingested (often months post
  IPO), so the pending SQL would flag it `no_financial_facts` every hour and
  re-run the full ~60-endpoint onboard — ~720 FMP calls/day against the 750/day
  cap. We DEFER such tickers to a daily cadence (keyed off the most recent
  fmp_endpoint_status.last_pulled) rather than skipping them: the first onboard
  still runs (no prior fetch row), and once FMP has data the next daily
  re-check onboards it. This is the chosen fix among the three options weighed
  (vs. a save_fmp_data skip-window or hard skip) because it kills both the
  hourly subprocess churn AND the FMP-call waste in one place while preserving
  "auto-onboard the moment data arrives". The Issue-3 fix (recording accessible-
  but-empty endpoints as `empty` not `forbidden`) is the complementary signal
  that makes "is this IPO covered yet?" answerable.

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
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from pipeline.cadence_policy import (  # noqa: E402
    ESTIMATED_FMP_CALLS_PER_ONBOARD,
    check_onboarding_budget,
)
from pipeline.queries import open_db  # noqa: E402

_DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
_HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"
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
    -- ETFs have no FMP financial statements and no DCF by design (they onboard
    -- via the published-data lane: N-PORT + issuer overlay — src/etf_sources/).
    -- Without this guard an evaluation ETF is eternally 'no_financial_facts'
    -- and re-runs the full ~40-endpoint FMP onboard every hour, forever —
    -- the 2026-07 free-tier quota starvation.
    WHEN tc.instrument_type != 'etf'
         AND (SELECT COUNT(*) FROM financial_facts ff WHERE UPPER(ff.ticker) = UPPER(tc.ticker)) = 0
      THEN 'no_financial_facts'
    WHEN tc.instrument_type != 'etf'
         AND (SELECT COUNT(*) FROM dcf_runs d WHERE UPPER(d.ticker) = UPPER(tc.ticker)) = 0
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
    OR (
      tc.instrument_type != 'etf'
      AND (SELECT COUNT(*) FROM financial_facts ff WHERE UPPER(ff.ticker) = UPPER(tc.ticker)) = 0
    )
    OR (
      tc.instrument_type != 'etf'
      AND (SELECT COUNT(*) FROM dcf_runs d WHERE UPPER(d.ticker) = UPPER(tc.ticker)) = 0
    )
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


# Recently-IPO'd tickers (flagged in their holdings JSON) have near-zero FMP
# coverage until their first 10-Q is ingested — often months after IPO. The
# pending SQL flags them `no_financial_facts` every run, so without a guard the
# hourly cron re-runs the full ~60-endpoint onboard for them, burning ~720
# FMP calls/day against the 750/day cap. We back them off to a DAILY cadence
# rather than skipping outright, so the moment FMP ingests data the next daily
# re-check picks it up (see _is_recently_ipod / apply_ipo_backoff).
_RECENTLY_IPOD_RETRY_HOURS = 24


def _is_recently_ipod(ticker: str, holdings_dir: Path) -> bool:
    """True iff micro_thesis/holdings/<TICKER>.json sets recently_ipod: true."""
    path = holdings_dir / f"{ticker.upper()}.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return bool(cast("dict[str, object]", data).get("recently_ipod"))


def _last_fmp_attempt(conn: sqlite3.Connection, ticker: str) -> datetime | None:
    """MAX(last_pulled) across this ticker's fmp_endpoint_status rows, parsed to
    a naive-local datetime (matching save_fmp_data's writer). Returns None when
    the ticker was never fetched or the table/value is absent or unparseable —
    callers treat None as 'never attempted' (do NOT defer)."""
    try:
        cur = conn.execute(
            "SELECT MAX(last_pulled) AS m FROM fmp_endpoint_status WHERE UPPER(ticker) = UPPER(?)",
            (ticker,),
        )
        row = cur.fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    raw = row["m"] if isinstance(row, sqlite3.Row) else row[0]
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def apply_ipo_backoff(
    pending: list[tuple[str, str]],
    db_path: Path,
    holdings_dir: Path,
    *,
    now: datetime | None = None,
    retry_hours: int = _RECENTLY_IPOD_RETRY_HOURS,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Split `pending` into (to_process, deferred).

    A ticker is deferred when ALL of:
      - its pending_reason needs the heavy onboard chain (not no_commitments —
        that stage is cheap and FMP-free), AND
      - its holdings JSON has recently_ipod: true, AND
      - its most recent FMP fetch (fmp_endpoint_status.last_pulled) was less
        than `retry_hours` ago.

    Never-fetched recently-IPO'd tickers (no fmp_endpoint_status rows) are NOT
    deferred — the first onboard must run, and the daily re-check thereafter
    picks up coverage the moment FMP ingests it. Non-IPO tickers are never
    deferred (preserves the hourly cadence for the rest of the universe).
    """
    now = now or datetime.now()
    cutoff = timedelta(hours=retry_hours)
    conn = open_db(db_path)
    try:
        to_process: list[tuple[str, str]] = []
        deferred: list[tuple[str, str]] = []
        for ticker, reason in pending:
            if reason != _COMMITMENT_ONLY_REASON and _is_recently_ipod(ticker, holdings_dir):
                last = _last_fmp_attempt(conn, ticker)
                if last is not None and (now - last) < cutoff:
                    deferred.append((ticker, reason))
                    continue
            to_process.append((ticker, reason))
        return to_process, deferred
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


def _remaining_fmp_budget() -> int:
    """Best-effort read of today's remaining FMP tier budget.

    Imports refresh_cache lazily so cron-only environments that don't have
    FMP_API_KEY set still let `--skip-budget-gate` runs work. Returns a
    very large int (effectively unlimited) when the budget tracker is
    unavailable so the gate only fires when the data is reliable.
    """
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "execution"))
        import refresh_cache  # noqa: E402

        tier = refresh_cache.resolve_tier(None)
        return refresh_cache.remaining_budget(tier)
    except (ImportError, SystemExit, ValueError, KeyError):
        # Can't read the tier or budget file — don't block the run.
        return 10**9


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
    ap.add_argument(
        "--skip-budget-gate",
        action="store_true",
        help="Skip the pre-flight FMP tier-cap check. Use with caution — bulk "
        "onboarding on the basic tier (250 calls/day) can blow the cap "
        "halfway through.",
    )
    ap.add_argument(
        "--calls-per-onboard",
        type=int,
        default=ESTIMATED_FMP_CALLS_PER_ONBOARD,
        help=f"Estimated FMP calls per ticker for the gate (default: "
        f"{ESTIMATED_FMP_CALLS_PER_ONBOARD})",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="[onboard_pending] %(message)s")
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = _LOG_DIR / f"onboard_pending_{stamp}.log"

    pending_all = find_pending_tickers(Path(args.db))
    # Back recently-IPO'd, near-zero-coverage tickers off to a daily cadence so
    # they don't re-run the full onboard hourly. Deferred tickers are surfaced
    # in the report (not silently dropped) and still re-checked once a day.
    pending, deferred = apply_ipo_backoff(pending_all, Path(args.db), _HOLDINGS_DIR)
    deferred_payload = [
        {"ticker": t, "reason": r, "deferred": "recently_ipod_daily_cadence"} for t, r in deferred
    ]
    if deferred:
        log.info(
            "deferred %d recently-IPO'd ticker(s) to daily cadence: %s",
            len(deferred),
            ", ".join(t for t, _ in deferred),
        )

    if not pending:
        report = {
            "run_id": stamp,
            "pending_count": 0,
            "deferred": deferred_payload,
            "results": [],
            "log": str(log_path),
        }
        print(json.dumps(report, indent=2))
        return 0

    if args.max > 0:
        pending = pending[: args.max]

    if args.dry_run:
        print(
            json.dumps(
                {
                    "run_id": stamp,
                    "pending_count": len(pending),
                    "tickers": [{"ticker": t, "reason": r} for t, r in pending],
                    "deferred": deferred_payload,
                },
                indent=2,
            )
        )
        return 0

    # Skip the gate for fmp-skipped runs (no FMP calls fire) and for
    # commitment-only batches (heavy stages already done).
    needs_budget_gate = (
        not args.skip_budget_gate
        and not args.skip_fmp
        and any(r != _COMMITMENT_ONLY_REASON for _, r in pending)
    )
    if needs_budget_gate:
        full_onboards = sum(1 for _, r in pending if r != _COMMITMENT_ONLY_REASON)
        remaining = _remaining_fmp_budget()
        allowed, reason = check_onboarding_budget(
            pending_count=full_onboards,
            remaining_calls=remaining,
            calls_per_onboard=args.calls_per_onboard,
        )
        if not allowed:
            report = {
                "run_id": stamp,
                "blocked": True,
                "block_reason": "fmp_budget_gate",
                "detail": reason,
                "pending_count": len(pending),
                "log": str(log_path),
            }
            print(json.dumps(report, indent=2))
            log.warning("budget gate blocked run: %s", reason)
            return 2
        log.info("budget gate passed: %s", reason)

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
        outcomes = " ".join(f"{s.stage.split('_')[0]}={s.outcome.value}" for s in result.stages)
        log.info("  %s done in %.1fs — %s", ticker, result.elapsed_seconds, outcomes)

    report = {
        "run_id": stamp,
        "pending_count": len(pending),
        "deferred": deferred_payload,
        "log": str(log_path),
        "results": [_result_to_dict(r) for r in results],
    }
    print(json.dumps(report, indent=2))

    # Exit code: non-zero if every onboard subprocess failed (signals real problem).
    # Pure no-commitments runs skip onboard, so they never count toward this signal.
    full_chain_results = [r for r in results if r.pending_reason != _COMMITMENT_ONLY_REASON]
    onboard_failures = sum(
        1 for r in full_chain_results if r.stages[0].outcome is StageOutcome.FAILED
    )
    return 1 if full_chain_results and onboard_failures == len(full_chain_results) else 0


if __name__ == "__main__":
    sys.exit(main())
