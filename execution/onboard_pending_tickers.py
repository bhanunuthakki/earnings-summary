"""Catch up active portfolio, watchlist and evaluation tickers.

Missing instrument classification or source facts queues onboarding. Missing
DCF queues compute only when retained valuation inputs exist. It does not
replay source acquisition. Missing owner thesis skips thesis evaluation; it
does not block company-reported data. Transcript work uses the existing typed
immutable scan-coverage classifier.

FMP budget exhaustion and recently-IPO daily backoff defer only FMP. Independent
SEC CompanyFacts acquisition still runs. ETF source selection remains separate.
These operational queue checks do not certify report-grade completeness.

``--project-root`` selects retained artifacts. ``--db`` selects the configured
database. Runtime configuration resolves before logs, discovery or child writes.
The hourly scheduler uses one portfolio-db writer lock per ticker. Native stages
retain their own immutable source and replay accounting.

Usage:
    python execution/onboard_pending_tickers.py --project-root STATE --db DB
    python execution/onboard_pending_tickers.py --dry-run
    python execution/onboard_pending_tickers.py --skip-fmp
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

import db
from compute.say_do_extractor import transcripts_without_scan_receipt
from db_paths import require_db_path
from pipeline.cadence_policy import (
    ESTIMATED_FMP_CALLS_PER_ONBOARD,
    check_onboarding_budget,
)
from pipeline.commitment_scan_receipts import scan_receipt_schema_available
from pipeline.queries import open_db
from provenance.selection import selected_transcripts_relation
from runtime.job_runtime import JobAlreadyRunningError, JobLock
from runtime.python_process import ensure_managed_python_argv, managed_python_prefix
from runtime.secrets import load_project_env

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_STATE_ROOT = PROJECT_ROOT
_DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
_HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"
_LOG_DIR = PROJECT_ROOT / ".tmp" / "cron_logs"

# When pending_reason is 'no_commitments', only the commitment-extract stage
# needs to run — the heavy onboard/eval/DCF stages are no-ops.
_COMMITMENT_ONLY_REASON = "no_commitments"

log = logging.getLogger("onboard_pending")


def configure_runtime(project_root: Path, database_path: Path | None) -> None:
    """Resolve the retained state before discovery, logs or child writes."""
    global _STATE_ROOT, _DB_PATH, _HOLDINGS_DIR, _LOG_DIR
    state_root = project_root.expanduser().resolve()
    load_project_env(state_root)
    approved_database = database_path or os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    if not approved_database:
        raise RuntimeError("An explicit or configured portfolio database is required")
    resolved_database = require_db_path(approved_database)
    _STATE_ROOT = state_root
    _DB_PATH = resolved_database
    _HOLDINGS_DIR = state_root / "micro_thesis" / "holdings"
    _LOG_DIR = state_root / ".tmp" / "cron_logs"
    os.environ["EARNINGS_SUMMARY_DB_PATH"] = str(resolved_database)
    db.set_db_path(resolved_database, state_root=state_root)


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


# Excludes transcripts already recorded in commitment_scan_log — a
# zero-commitment scan is a durable outcome the extractor never retries, so
# such transcripts must not keep their ticker in the queue.
_SCAN_LOG_FILTER = (
    " AND NOT EXISTS (SELECT 1 FROM commitment_scan_log l WHERE l.transcript_id = t.id)"
)


def _commitments_pending_predicate(conn: sqlite3.Connection, scan_log_exists: bool) -> str:
    """Build the legacy/preliminary SQL no-commitments candidate arm.

    ``find_pending_tickers`` replaces this arm with the shared typed coverage
    result whenever the active immutable receipt schema is available.
    """
    scan_filter = _SCAN_LOG_FILTER if scan_log_exists else ""
    transcripts_relation = selected_transcripts_relation(conn).sql
    return f"""(
      EXISTS (
        SELECT 1 FROM {transcripts_relation} t
        WHERE UPPER(t.ticker) = UPPER(tc.ticker){scan_filter}
      )
      AND NOT EXISTS (
        SELECT 1 FROM management_commitments mc WHERE UPPER(mc.ticker) = UPPER(tc.ticker)
      )
      AND EXISTS (
        SELECT 1 FROM kpi_definitions k WHERE UPPER(k.ticker) = UPPER(tc.ticker)
      )
    )"""


def _pending_sql(conn: sqlite3.Connection, scan_log_exists: bool) -> str:
    commitments_pending = _commitments_pending_predicate(conn, scan_log_exists)
    return f"""
SELECT
  tc.ticker,
  {commitments_pending} AS commitments_pending,
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
    WHEN {commitments_pending}
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
    OR {commitments_pending}
  )
ORDER BY tc.added_at, tc.ticker
"""


def _scan_log_exists(conn: sqlite3.Connection) -> bool:
    """True when the commitment_scan_log table (migration 0129) exists.
    Selection degrades gracefully on a pre-0129 DB, matching the extractor's
    scan_log_available: every unscanned-looking transcript stays pending."""
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'commitment_scan_log'"
    )
    return cur.fetchone() is not None


def find_pending_tickers(db_path: Path) -> list[tuple[str, str]]:
    """Return [(ticker, pending_reason), ...] for every P+W ticker still missing onboard data."""
    conn = open_db(db_path)
    try:
        rows = conn.execute(_pending_sql(conn, _scan_log_exists(conn))).fetchall()
        source_or_compute_rows = [
            (str(row["ticker"]), str(row["pending_reason"]))
            for row in rows
            if row["pending_reason"] != "no_dcf_run" or _has_valuation_inputs(str(row["ticker"]))
        ]
        if not scan_receipt_schema_available(conn):
            suppressed = {ticker for ticker, _reason in source_or_compute_rows}
            return source_or_compute_rows + [
                (str(row["ticker"]), _COMMITMENT_ONLY_REASON)
                for row in rows
                if str(row["ticker"]) not in suppressed and row["commitments_pending"]
            ]

        scan_tickers = {
            ticker for _transcript_id, ticker, _period_end in transcripts_without_scan_receipt(conn)
        }
        reason_by_ticker = {
            ticker: reason
            for ticker, reason in source_or_compute_rows
            if reason != _COMMITMENT_ONLY_REASON
        }
        ordered_tickers = [
            str(row["ticker"])
            for row in conn.execute(
                f"SELECT ticker FROM tracked_companies "  # nosec B608 -- constant enum SQL
                f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} ORDER BY added_at,ticker"
            )
        ]
        return [
            (
                ticker,
                reason_by_ticker.get(ticker, _COMMITMENT_ONLY_REASON),
            )
            for ticker in ordered_tickers
            if ticker in reason_by_ticker or ticker in scan_tickers
        ]
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
        fh.write(f"\n[{stage}] cmd: {' '.join(cmd)}\n".encode())
        fh.flush()
        proc = subprocess.run(
            ensure_managed_python_argv(PROJECT_ROOT, cmd),
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


def _has_valuation_inputs(ticker: str) -> bool:
    """Queue owner-conditioned compute only when its existing input exists."""
    if (_STATE_ROOT / "dcf" / f"{ticker.upper()}.xlsx").is_file():
        return True
    holdings_path = _HOLDINGS_DIR / f"{ticker.upper()}.json"
    assumptions_path = _STATE_ROOT / "data" / "dcf_assumptions" / f"{ticker.upper()}.json"
    for path in (holdings_path, assumptions_path):
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        data = cast("dict[str, object]", raw)
        wacc = data.get("wacc")
        if isinstance(wacc, (int, float)) and not isinstance(wacc, bool) and wacc > 0:
            return True
        model = data.get("valuation_model")
        if isinstance(model, str) and model in {
            "fcff_dcf",
            "bank_excess_return",
            "holdco_sotp",
            "fintech_sotp",
            "platform_dcf",
            "meli_platform_sotp",
        }:
            return True
        redesign = data.get("redesign")
        if isinstance(redesign, dict):
            redesign_data = cast("dict[str, object]", redesign)
            model = redesign_data.get("valuation_model")
            if (
                redesign_data
                and redesign_data.get("dcf_applicable") is not False
                and model != "none"
                and model != "new"
            ):
                return True
    return False


def onboard_one(
    ticker: str,
    pending_reason: str,
    *,
    skip_fmp: bool,
    skip_commitments: bool,
    log_path: Path,
    skip_sec: bool = False,
) -> TickerResult:
    """Run the appropriate stage chain for a single ticker. Returns a structured result.

    Stage subset depends on pending_reason:
      - no_commitments        -> commitment_extract only (heavy stages skipped)
      - any other reason      -> full chain (onboard + eval + DCF + commitment_extract)

    All stages best-effort: failures are surfaced in the result, not raised.
    """
    started = datetime.now(UTC)
    stages: list[StageResult] = []
    is_commitment_only = pending_reason == _COMMITMENT_ONLY_REASON

    if is_commitment_only:
        stages.append(_skipped("onboard_ticker", "ticker already onboarded"))
        stages.append(_skipped("run_thesis_evaluator", "ticker already evaluated"))
        stages.append(_skipped("refresh_dcf", "ticker already has DCF"))
    else:
        onboard_cmd = [
            *managed_python_prefix(PROJECT_ROOT),
            "execution/onboard_ticker.py",
            "--ticker",
            ticker,
            "--project-root",
            str(_STATE_ROOT),
            "--db",
            str(_DB_PATH),
        ]
        if skip_fmp:
            onboard_cmd.append("--skip-fmp")
        if skip_sec:
            onboard_cmd.append("--skip-sec")
        stages.append(
            _skipped("onboard_ticker", "source acquisition is not missing")
            if pending_reason == "no_dcf_run"
            else _run_subprocess(onboard_cmd, "onboard_ticker", log_path)
        )

        # run_thesis_evaluator is best-effort — missing holdings JSON returns non-zero
        # but should not abort the rest of the chain.
        eval_cmd = [
            *managed_python_prefix(PROJECT_ROOT),
            "execution/run_thesis_evaluator.py",
            "--ticker",
            ticker,
            "--db",
            str(_DB_PATH),
            "--holdings-dir",
            str(_HOLDINGS_DIR),
        ]
        stages.append(
            _run_subprocess(eval_cmd, "run_thesis_evaluator", log_path)
            if (_HOLDINGS_DIR / f"{ticker.upper()}.json").is_file()
            else _skipped("run_thesis_evaluator", "owner thesis input unavailable")
        )

        # refresh_dcf replaces the old batch_dcf path: seeds dcf/<TICKER>.xlsx
        # if missing, refreshes its Historicals, then re-runs the PV calc.
        dcf_cmd = [
            *managed_python_prefix(PROJECT_ROOT),
            "execution/refresh_dcf.py",
            "--ticker",
            ticker,
            "--repo-root",
            str(_STATE_ROOT),
        ]
        stages.append(
            _run_subprocess(dcf_cmd, "refresh_dcf", log_path)
            if _has_valuation_inputs(ticker)
            else _skipped("refresh_dcf", "existing valuation input unavailable")
        )

    if skip_commitments:
        stages.append(_skipped("extract_commitments", "--skip-commitments flag"))
    else:
        # Auto-extract any pending commitments from this ticker's transcripts.
        # Idempotent: transcripts already with a commitment row are skipped.
        # Best-effort: missing LLM auth / no transcripts is non-fatal.
        commit_cmd = [
            *managed_python_prefix(PROJECT_ROOT),
            "execution/extract_commitments_from_transcript.py",
            "--auto",
            "--ticker",
            ticker,
            "--db",
            str(_DB_PATH),
        ]
        stages.append(_run_subprocess(commit_cmd, "extract_commitments", log_path))

    elapsed = (datetime.now(UTC) - started).total_seconds()
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
        from execution import refresh_cache

        tier = refresh_cache.resolve_tier(None)
        prior_cache_dir = refresh_cache.CACHE_DIR
        try:
            refresh_cache.CACHE_DIR = _STATE_ROOT / ".tmp" / "cacher"
            return refresh_cache.remaining_budget(tier)
        finally:
            refresh_cache.CACHE_DIR = prior_cache_dir
    except (ImportError, SystemExit, ValueError, KeyError):
        # Can't read the tier or budget file — don't block the run.
        return 10**9


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, help="Explicit configured portfolio database")
    ap.add_argument(
        "--project-root",
        "--repo-root",
        dest="project_root",
        type=Path,
        default=PROJECT_ROOT,
        help="Private state/artifact root; code stays in the managed checkout",
    )
    ap.add_argument("--dry-run", action="store_true", help="List pending tickers and exit")
    ap.add_argument("--max", type=int, default=0, help="Limit to first N tickers (0 = no limit)")
    ap.add_argument(
        "--skip-fmp",
        action="store_true",
        help="Skip FMP fetch; independent SEC acquisition still runs",
    )
    ap.add_argument("--skip-sec", action="store_true", help="Skip independent SEC CompanyFacts")
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
    configure_runtime(args.project_root, args.db)

    logging.basicConfig(level=logging.INFO, format="[onboard_pending] %(message)s")
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Microsecond resolution (not just %Y%m%dT%H%M%SZ) so this stamp can never
    # collide with cron/run_onboard_pending.bat's independently-computed %TS%
    # (a separate `Get-Date` call, second resolution only). A same-second
    # collision makes log_path identical to the .bat's LOGFILE, which cmd.exe
    # already holds open via `>>` redirect for this whole process — this
    # script's own `open(log_path, "ab")` in _run_subprocess then hits
    # PermissionError (Windows sharing violation) and every stage after the
    # first pending ticker crashes. Confirmed in prod: every hourly run since
    # 2026-07-16T09:17Z died on the first ticker (FIGR) with exactly this
    # traceback. run_id keeps the second-resolution stamp (human-readable,
    # unaffected) — only the log filename needs the extra entropy.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    log_path = _LOG_DIR / f"onboard_pending_{log_stamp}.log"

    pending_all = find_pending_tickers(_DB_PATH)
    # Apply the existing daily FMP cadence without deferring SEC or local work.
    _fmp_ready, deferred = apply_ipo_backoff(pending_all, _DB_PATH, _HOLDINGS_DIR)
    # IPO cadence belongs to FMP. It cannot suppress independent SEC capture.
    pending = pending_all
    ipo_fmp_deferred = {ticker for ticker, _reason in deferred}
    deferred_payload = [
        {"ticker": t, "reason": r, "deferred": "fmp_recently_ipod_daily_cadence"}
        for t, r in deferred
    ]
    if deferred:
        log.info(
            "deferred FMP for %d recently-IPO'd ticker(s) to daily cadence: %s",
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
        and any(r != _COMMITMENT_ONLY_REASON and t not in ipo_fmp_deferred for t, r in pending)
    )
    fmp_deferred: dict[str, object] | None = None
    if needs_budget_gate:
        full_onboards = sum(
            1 for t, r in pending if r != _COMMITMENT_ONLY_REASON and t not in ipo_fmp_deferred
        )
        remaining = _remaining_fmp_budget()
        allowed, reason = check_onboarding_budget(
            pending_count=full_onboards,
            remaining_calls=remaining,
            calls_per_onboard=args.calls_per_onboard,
        )
        if not allowed:
            fmp_deferred = {
                "reason": "fmp_budget_gate",
                "detail": reason,
            }
            log.info("FMP deferred; independent SEC and local work continue: %s", reason)
        else:
            log.info("budget gate passed: %s", reason)

    log.info("starting run %s — %d pending tickers — log: %s", stamp, len(pending), log_path)
    results: list[TickerResult] = []
    for ticker, reason in pending:
        log.info("processing %s (%s)", ticker, reason)
        # portfolio-db is claimed PER TICKER, not for the whole run. A full
        # run regularly outlives its hourly trigger (01:17->03:20 observed on
        # 2026-08-03), and holding the DB write set across all of it starved
        # every other scheduled writer -- 13 jobs skipped_locked in one day,
        # including four consecutive nightly backups. Discovery, the budget
        # gate, and all network/LLM preparation above run OUTSIDE the lock;
        # only each ticker's bounded mutation chain runs inside it, so the
        # set is free between tickers for anything that was waiting.
        try:
            with JobLock(PROJECT_ROOT, "onboard-pending", ["portfolio-db"], wait_s=600.0):
                result = onboard_one(
                    ticker,
                    reason,
                    skip_fmp=(
                        args.skip_fmp or fmp_deferred is not None or ticker in ipo_fmp_deferred
                    ),
                    skip_sec=args.skip_sec,
                    skip_commitments=args.skip_commitments,
                    log_path=log_path,
                )
        except JobAlreadyRunningError as exc:
            # A long holder (db_gc, the morning pipeline) outlasted the wait:
            # skip THIS ticker and keep going -- it stays pending and the next
            # hourly run retries it. One busy writer must not abort the list.
            log.warning("  %s skipped: %s", ticker, exc)
            continue
        results.append(result)
        outcomes = " ".join(f"{s.stage.split('_')[0]}={s.outcome.value}" for s in result.stages)
        log.info("  %s done in %.1fs — %s", ticker, result.elapsed_seconds, outcomes)

    report = {
        "run_id": stamp,
        "pending_count": len(pending),
        "deferred": deferred_payload,
        "fmp_deferred": fmp_deferred,
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
